from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from claude_p.client import ClaudePClient, ClaudeRunConfig
from claude_p.session_manager import ClaudePSessionManager


class _FakeStdin:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._buffer = bytearray("".join(lines).encode("utf-8"))

    async def readline(self) -> bytes:
        if not self._buffer:
            return b""
        separator_at = self._buffer.find(b"\n")
        if separator_at < 0:
            line = bytes(self._buffer)
            self._buffer.clear()
            return line
        line = bytes(self._buffer[: separator_at + 1])
        del self._buffer[: separator_at + 1]
        return line

    async def read(self, n: int = -1) -> bytes:
        if not self._buffer:
            return b""
        if n < 0 or n >= len(self._buffer):
            chunk = bytes(self._buffer)
            self._buffer.clear()
            return chunk
        chunk = bytes(self._buffer[:n])
        del self._buffer[:n]
        return chunk


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], stderr_lines: list[str], returncode: int) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


# Reálné řádky z `npx claude-p ... --output-format stream-json` (prokládaný transkript).
_SID = "6e0f0c6e-b645-4f83-aa13-6eb7ee453f07"
_LOG_LINES = [
    {"type": "mode", "mode": "normal", "sessionId": _SID},
    {"type": "permission-mode", "permissionMode": "default", "sessionId": _SID},
    {"type": "file-history-snapshot", "messageId": "m1", "snapshot": {}, "isSnapshotUpdate": False},
    {"type": "user", "message": {"role": "user", "content": "spust pwd"}, "sessionId": _SID, "cwd": "/root"},
    {"type": "attachment", "attachment": {"type": "deferred_tools_delta"}, "sessionId": _SID},
    {
        "type": "assistant",
        "message": {
            "model": "claude-haiku-4-5-20251001",
            "id": "msg_01BsDqUScddr7vNCvGh3dmre",
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {"command": "pwd"}}],
            "usage": {"input_tokens": 10, "output_tokens": 142, "cache_read_input_tokens": 12231},
        },
        "sessionId": _SID,
        "cwd": "/root",
    },
    {
        "type": "user",
        "message": {"role": "user", "content": [{"tool_use_id": "toolu_01", "type": "tool_result", "content": "/root", "is_error": False}]},
        "sessionId": _SID,
        "cwd": "/root",
    },
    {
        "type": "assistant",
        "message": {
            "model": "claude-haiku-4-5-20251001",
            "id": "msg_011FypCJDP7TsmyEMbxjQ9BW",
            "role": "assistant",
            "content": [{"type": "text", "text": "Aktuální pracovní adresář je `/root`."}],
            "usage": {"input_tokens": 8, "output_tokens": 40, "cache_read_input_tokens": 19317},
        },
        "sessionId": _SID,
        "cwd": "/root",
    },
    {"type": "system", "subtype": "turn_duration", "durationMs": 4295, "sessionId": _SID},
    {
        "type": "result",
        "subtype": "success",
        "session_id": _SID,
        "result": "Aktuální pracovní adresář je `/root`.",
        "is_error": False,
        "total_cost_usd": 0,
        "usage": {"input_tokens": 36, "output_tokens": 364, "cache_read_input_tokens": 63096},
    },
]


def _run_stream(lines: list[dict[str, Any]], monkeypatch) -> list[dict[str, Any]]:
    stdout = [json.dumps(line) + "\n" for line in lines]

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(stdout_lines=stdout, stderr_lines=[], returncode=0)

    monkeypatch.setattr("claude.client.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect() -> list[dict[str, Any]]:
        client = ClaudePClient(config=ClaudeRunConfig(command="claude-p", cwd="/work"))
        return [{"type": event.type, **event.data} async for event in client.stream("spust pwd")]

    return asyncio.run(collect())


def test_mode_event_synthesizes_session_start(monkeypatch) -> None:
    events = _run_stream(_LOG_LINES, monkeypatch)

    # První session_start přijde z `mode` řádku (session_id, model ještě neznáme).
    first_start = next(e for e in events if e["type"] == "session_start")
    assert first_start["session_id"] == _SID
    assert first_start["model"] is None


def test_session_start_reemitted_with_model_from_first_assistant(monkeypatch) -> None:
    events = _run_stream(_LOG_LINES, monkeypatch)

    starts = [e for e in events if e["type"] == "session_start"]
    # Přesně dva: jeden z `mode` (bez modelu) a jeden z první assistant zprávy (s modelem).
    assert len(starts) == 2
    assert starts[1]["model"] == "claude-haiku-4-5-20251001"
    assert starts[1]["session_id"] == _SID


def test_assistant_tool_result_and_result_normalize_like_claude(monkeypatch) -> None:
    events = _run_stream(_LOG_LINES, monkeypatch)
    types = [e["type"] for e in events]

    assert "tool_use" in types
    assert "tool_result" in types
    assert "text" in types

    tool_use = next(e for e in events if e["type"] == "tool_use")
    assert tool_use["id"] == "toolu_01"
    assert tool_use["name"] == "Bash"

    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["tool_use_id"] == "toolu_01"
    assert tool_result["content"] == "/root"
    assert tool_result["is_error"] is False

    result = next(e for e in events if e["type"] == "result")
    assert result["session_id"] == _SID
    assert result["summary"] == "Aktuální pracovní adresář je `/root`."
    assert result["is_error"] is False


def test_prompt_echo_user_string_content_does_not_emit_tool_result(monkeypatch) -> None:
    # `user` echo promptu má `content` jako string — nesmí to spadnout ani vyrobit tool_result.
    events = _run_stream(_LOG_LINES, monkeypatch)
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 1  # jen ten reálný z Bash běhu


def test_session_manager_builds_claude_p_command() -> None:
    from common.config import Settings
    from common.models import AdapterOptionsPayload, AgentExecutionContextPayload

    settings = Settings(
        host="127.0.0.1",
        port=8002,
        worktree_root=Path("/var/www/worktrees"),
        public_base_url=None,
        agentis_endpoint=None,
        agentis_token=None,
    )
    manager = ClaudePSessionManager(settings=settings)
    context = AgentExecutionContextPayload(
        task_id="t1",
        run_id="r1",
        title="x",
        adapter=AdapterOptionsPayload(model="claude-haiku-4-5-20251001"),
    )
    sess = type("S", (), {"worktree": "/work", "context": context})()
    client = manager._build_client(sess, resume_id=None)

    assert client.config.command == "claude-p"
    assert client.config.model == "claude-haiku-4-5-20251001"
