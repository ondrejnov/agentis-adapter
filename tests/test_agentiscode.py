from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.agentiscode import run
from common.agentiscode import (
    AgentConfig,
    AgentEvent,
    AgentWrapper,
    _ClaudeTranslator,
    _OpenCodeTranslator,
    normalize_adapter,
    tool_title,
)
from claude.client import ClaudeEvent
from opencode.runner import OpenCodeEvent


# ---------------------------------------------------------------------------
# Fake subprocess plumbing (mirrors tests/test_opencode.py)
# ---------------------------------------------------------------------------


class _FakeStdin:
    def __init__(self) -> None:
        self.closed = False
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._buffer = bytearray("".join(lines).encode("utf-8"))

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
        self.pid = 4242

    async def wait(self) -> int:
        return self.returncode


def _fake_subprocess(stdout_lines: list[str]):
    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(stdout_lines=stdout_lines, stderr_lines=[], returncode=0)

    return fake_create_subprocess_exec


# ---------------------------------------------------------------------------
# Helpers / config
# ---------------------------------------------------------------------------


def test_normalize_adapter_aliases() -> None:
    assert normalize_adapter("OpenCode") == "opencode"
    assert normalize_adapter("cloud") == "claude"
    assert normalize_adapter("claudecode") == "claude"
    with pytest.raises(ValueError):
        normalize_adapter("gemini")


def test_tool_title_handles_both_naming_conventions() -> None:
    assert tool_title("Read", {"file_path": "/w/app/main.py"}) == "/w/app/main.py"
    assert tool_title("bash", {"command": "ls -la"}) == "ls -la"
    assert tool_title("bash", {"description": "list files", "command": "ls"}) == "list files"
    assert tool_title("mystery", {"x": 1}) == "mystery"


# ---------------------------------------------------------------------------
# Translators
# ---------------------------------------------------------------------------


def test_opencode_translator_emits_text_deltas_and_dedups_tools() -> None:
    translate = _OpenCodeTranslator()
    events: list[AgentEvent] = []
    for native in [
        OpenCodeEvent("session_start", {"session_id": "ses_1"}),
        OpenCodeEvent("part", {"part": {"id": "p1", "type": "text", "text": "Hel"}}),
        OpenCodeEvent("part", {"part": {"id": "p1", "type": "text", "text": "Hello"}}),
        OpenCodeEvent("tool_before", {"callID": "c1", "tool": "bash", "input": {"command": "ls"}}),
        OpenCodeEvent(
            "part",
            {
                "part": {
                    "id": "p2",
                    "type": "tool",
                    "callID": "c1",
                    "tool": "bash",
                    "state": {"status": "running", "input": {"command": "ls"}},
                }
            },
        ),
        OpenCodeEvent(
            "part",
            {
                "part": {
                    "id": "p2",
                    "type": "tool",
                    "callID": "c1",
                    "tool": "bash",
                    "state": {"status": "completed", "input": {"command": "ls"}, "output": "file.py"},
                }
            },
        ),
    ]:
        events.extend(translate(native))

    kinds = [(e.type, e.data.get("status")) for e in events]
    # session, text(Hel), text(lo), tool running (once), tool completed
    assert kinds == [
        ("session", None),
        ("text", None),
        ("text", None),
        ("tool", "running"),
        ("tool", "completed"),
    ]
    assert [e.data["text"] for e in events if e.type == "text"] == ["Hel", "lo"]
    completed = events[-1]
    assert completed.data["output"] == "file.py"
    assert completed.data["id"] == "c1"


def test_claude_translator_maps_tool_use_and_result() -> None:
    translate = _ClaudeTranslator()
    events: list[AgentEvent] = []
    for native in [
        ClaudeEvent("session_start", {"session_id": "s1", "model": "claude-x", "cwd": "/w"}),
        ClaudeEvent("text", {"text": "Hi"}),
        ClaudeEvent("tool_use", {"id": "t1", "name": "Read", "input": {"file_path": "/w/a.py"}}),
        ClaudeEvent("tool_result", {"tool_use_id": "t1", "content": "data", "is_error": False}),
        ClaudeEvent("result", {"session_id": "s1", "usage": {"input_tokens": 3}, "cost_usd": 0.02}),
    ]:
        events.extend(translate(native))

    types = [e.type for e in events]
    assert types == ["session", "text", "tool", "tool", "result"]
    assert events[0].data["provider"] == "anthropic"
    assert events[2].data == {
        "id": "t1",
        "name": "Read",
        "status": "running",
        "input": {"file_path": "/w/a.py"},
        "title": "/w/a.py",
    }
    assert events[3].data == {"id": "t1", "status": "completed", "output": "data"}
    assert events[4].data["usage"] == {"input_tokens": 3}


# ---------------------------------------------------------------------------
# Wrapper end-to-end (přes fake subprocess)
# ---------------------------------------------------------------------------


def test_wrapper_streams_opencode_and_synthesizes_result(monkeypatch) -> None:
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": "ses_9",
                "part": {"id": "p1", "messageID": "m1", "type": "text", "text": "Done"},
            }
        )
        + "\n",
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": "ses_9",
                "part": {
                    "type": "step-finish",
                    "tokens": {"input": 10, "output": 5, "cache": {"read": 0, "write": 0}},
                    "cost": 0.03,
                },
            }
        )
        + "\n",
    ]
    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", _fake_subprocess(lines))

    async def collect() -> list[AgentEvent]:
        wrapper = AgentWrapper(AgentConfig(adapter="opencode", model="haiku", cwd="/work"))
        return [event async for event in wrapper.stream("Do X")]

    events = asyncio.run(collect())
    types = [e.type for e in events]
    assert types == ["session", "text", "result"]
    # OpenCode nemá nativní result — wrapper ho dopočítá z runner.last_usage.
    result = events[-1]
    assert result.data["usage"]["input_tokens"] == 10
    assert result.data["cost_usd"] == 0.03
    assert result.data["is_error"] is False


def test_wrapper_streams_claude_with_native_result(monkeypatch) -> None:
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x", "cwd": "/w"}) + "\n",
        json.dumps({"type": "assistant", "session_id": "s1", "message": {"content": [{"type": "text", "text": "Hi"}]}})
        + "\n",
        json.dumps(
            {
                "type": "result",
                "session_id": "s1",
                "usage": {"input_tokens": 3, "output_tokens": 2},
                "total_cost_usd": 0.01,
                "result": "ok",
            }
        )
        + "\n",
    ]
    monkeypatch.setattr("claude.client.asyncio.create_subprocess_exec", _fake_subprocess(lines))

    async def collect() -> list[AgentEvent]:
        wrapper = AgentWrapper(AgentConfig(adapter="cloud", model="claude-x", cwd="/w"))
        return [event async for event in wrapper.stream("Do X")]

    events = asyncio.run(collect())
    assert [e.type for e in events] == ["session", "text", "result"]
    assert events[-1].data["session_id"] == "s1"
    assert events[-1].data["is_error"] is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_json_mode_emits_json_lines(monkeypatch, capsys) -> None:
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": "ses_1",
                "part": {"id": "p1", "messageID": "m1", "type": "text", "text": "Hello"},
            }
        )
        + "\n",
    ]
    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", _fake_subprocess(lines))

    exit_code = run(["--adapter", "opencode", "--json", "udelej", "X"])

    assert exit_code == 0
    out_lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    types = [entry["type"] for entry in out_lines]
    assert types == ["session", "text", "result"]
    assert out_lines[1]["text"] == "Hello"


def test_cli_requires_prompt(monkeypatch) -> None:
    # stdin není TTY a je prázdný → žádný prompt → argparse error (SystemExit 2)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with pytest.raises(SystemExit):
        run(["--adapter", "opencode"])


def test_cli_task_id_requires_agentis_api(monkeypatch) -> None:
    monkeypatch.delenv("AGENTIS_ENDPOINT", raising=False)
    with pytest.raises(SystemExit):
        run(["--adapter", "opencode", "--task-id", "task-1", "udelej", "X"])


def test_cli_task_id_drives_telemetry(monkeypatch) -> None:
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": "ses_1",
                "part": {"id": "p1", "messageID": "m1", "type": "text", "text": "Hello"},
            }
        )
        + "\n",
    ]
    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", _fake_subprocess(lines))

    events: dict[str, Any] = {"started": False, "handled": 0, "finished": False, "kwargs": None}

    class FakeTelemetry:
        def __init__(self, **kwargs: Any) -> None:
            events["kwargs"] = kwargs

        def start(self) -> str:
            events["started"] = True
            return "run-1"

        def handle(self, event: AgentEvent) -> None:
            events["handled"] += 1

        def finish(self) -> None:
            events["finished"] = True

        def close(self) -> None:
            events["closed"] = True

    monkeypatch.setattr("app.agentiscode.AgentisTelemetry", FakeTelemetry)

    exit_code = run(
        [
            "--adapter",
            "opencode",
            "--task-id",
            "task-1",
            "--agentis-api",
            "http://agentis.local/api",
            "--agentis-token",
            "secret",
            "udelej",
            "X",
        ]
    )

    assert exit_code == 0
    assert events["started"] and events["finished"] and events["closed"]
    assert events["handled"] >= 1
    assert events["kwargs"]["task_id"] == "task-1"
    assert events["kwargs"]["endpoint"] == "http://agentis.local/api"
    assert events["kwargs"]["token"] == "secret"
    assert events["kwargs"]["adapter"] == "opencode"
    assert events["kwargs"]["last_message_to_comment"] is False


def test_cli_last_message_to_comment_enables_final_comment(monkeypatch) -> None:
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": "ses_1",
                "part": {"id": "p1", "messageID": "m1", "type": "text", "text": "Hello"},
            }
        )
        + "\n",
    ]
    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", _fake_subprocess(lines))

    events: dict[str, Any] = {"kwargs": None}

    class FakeTelemetry:
        def __init__(self, **kwargs: Any) -> None:
            events["kwargs"] = kwargs

        def start(self) -> str:
            return "run-1"

        def handle(self, event: AgentEvent) -> None:
            return None

        def finish(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr("app.agentiscode.AgentisTelemetry", FakeTelemetry)

    exit_code = run(
        [
            "--adapter",
            "opencode",
            "--task-id",
            "task-1",
            "--agentis-api",
            "http://agentis.local/api",
            "--last-message-to-comment",
            "udelej",
            "X",
        ]
    )

    assert exit_code == 0
    assert events["kwargs"]["last_message_to_comment"] is True
