from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from common.config import Settings
from common.models import AdapterOptionsPayload, AgentExecutionContextPayload
from opencode.api import create_app, _DISPATCH
from tests.support import RpcTestClient
from opencode.adapter import OpenCodeAdapterService
from opencode.runner import OpenCodeRunner, OpenCodeEvent, OpenCodeRunConfig


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8003,
        "worktree_root": Path("/var/www/worktrees"),
        "public_base_url": "http://adapter.internal:8003",
        "agentis_endpoint": None,
        "agentis_token": None,
        "kubectl_command": "kubectl",
    }
    values.update(overrides)
    return Settings(**values)


def make_context(**overrides: Any) -> AgentExecutionContextPayload:
    payload: dict[str, Any] = {
        "run_id": "run-1",
        "task_id": "task-1",
        "title": "Implementace nove funkce",
        "description": "Popis ukolu",
        "project_slug": "agentis",
        "working_dir": "/var/www/repo",
        "adapter": AdapterOptionsPayload(agent="build", model="openrouter/openai/gpt-4.1-mini"),
    }
    payload.update(overrides)
    return AgentExecutionContextPayload(**payload)


# ---------------------------------------------------------------------------
# Fake subprocess plumbing (mirrors tests/test_claude_client.py)
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

    async def wait(self) -> int:
        return self.returncode


# ---------------------------------------------------------------------------
# OpenCodeRunConfig / client
# ---------------------------------------------------------------------------


def test_build_args_places_prompt_and_flags() -> None:
    args = OpenCodeRunConfig(model="openai/gpt-5", agent="build", variant="high").build_args(
        "precti prompt", prompt_file="/tmp/prompt.md"
    )
    assert args == [
        "run",
        "precti prompt",
        "--file",
        "/tmp/prompt.md",
        "--format",
        "json",
        "--dangerously-skip-permissions",
        "--model",
        "openai/gpt-5",
        "--agent",
        "build",
        "--variant",
        "high",
    ]


def test_build_args_resume_session() -> None:
    args = OpenCodeRunConfig(resume_session_id="ses_42").build_args("pokracuj", prompt_file="/tmp/prompt.md")
    assert "--session" in args
    assert args[args.index("--session") + 1] == "ses_42"


def test_stream_execs_local_opencode_directly_without_local_env_workflow(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = OpenCodeRunner(config=OpenCodeRunConfig(command="opencode", cwd="/work/project", model="haiku"))
        return [{"type": event.type, **event.data} async for event in client.stream("Do X")]

    events = asyncio.run(collect_events())

    assert events == []
    assert captured["args"][:2] == ("bash", "-c")
    assert captured["args"][2].startswith("exec opencode run 'Do X' --format json")
    assert "--file" not in captured["args"][2]
    assert "--model haiku" in captured["args"][2]
    assert captured["cwd"] == "/work/project"


def test_stream_wraps_local_opencode_with_local_env_workflow(monkeypatch, tmp_path) -> None:
    workflow_path = tmp_path / ".agentis" / "workflows" / "local-env.yaml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(
        "version: 1\n"
        "workflow:\n"
        "  env:\n"
        '    PATH: "[%WORKDIR%]/.venv/bin:$PATH"\n'
        "  steps:\n"
        "    - name: Ensure virtualenv\n"
        "      run: ensure-venv\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = OpenCodeRunner(config=OpenCodeRunConfig(command="opencode", cwd=str(tmp_path), model="haiku"))
        return [{"type": event.type, **event.data} async for event in client.stream("Do X")]

    events = asyncio.run(collect_events())

    assert events == []
    script = captured["args"][2]
    assert script.startswith("set -euo pipefail")
    assert f'export PATH="{tmp_path}/.venv/bin:$PATH"' in script
    assert "(\nensure-venv\n)" in script
    assert script.endswith("exec opencode run 'Do X' --format json --dangerously-skip-permissions --model haiku")


def test_stream_uses_temp_prompt_file_for_long_local_prompt(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(OpenCodeRunner, "_prompt_file_threshold_bytes", staticmethod(lambda: 10))

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = OpenCodeRunner(config=OpenCodeRunConfig(command="opencode"))
        return [{"type": event.type, **event.data} async for event in client.stream("x" * 11)]

    events = asyncio.run(collect_events())

    assert events == []
    assert (
        "exec opencode run 'Read the attached prompt file and follow its instructions exactly.'" in captured["args"][2]
    )
    prompt_match = re.search(r"--file (/tmp/opencode-prompt-[^ ]+\.md)", captured["args"][2])
    assert prompt_match is not None
    assert not Path(prompt_match.group(1)).exists()
    assert "x" * 11 not in captured["args"][2]


def test_stream_includes_stderr_in_nonzero_exit_error(monkeypatch) -> None:
    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(stdout_lines=[], stderr_lines=["boom\n"], returncode=1)

    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = OpenCodeRunner(config=OpenCodeRunConfig(command="/usr/bin/opencode"))
        return [{"type": event.type, **event.data} async for event in client.stream("Ahoj")]

    events = asyncio.run(collect_events())

    assert events[0] == {"type": "stderr", "line": "boom"}
    assert events[1]["type"] == "error"
    assert events[1]["exit_code"] == 1
    assert events[1]["message"].startswith("opencode skončil s kódem 1: boom")
    assert "stderr (posledních 20 řádků):\nboom" in events[1]["message"]


def test_stream_failure_message_prefers_context_over_end_marker(monkeypatch) -> None:
    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            stdout_lines=["non-json diagnostic\n"],
            stderr_lines=["real failure\n", "--- End ---\n"],
            returncode=1,
        )

    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = OpenCodeRunner(config=OpenCodeRunConfig(command="/usr/bin/opencode", cwd="/work/project"))
        return [{"type": event.type, **event.data} async for event in client.stream("tajny prompt")]

    events = asyncio.run(collect_events())
    error = next(event for event in events if event["type"] == "error")

    assert error["message"].startswith("opencode skončil s kódem 1: real failure")
    assert "opencode skončil s kódem 1: --- End ---" not in error["message"]
    assert "příkaz: /usr/bin/opencode run '<prompt>' --format json --dangerously-skip-permissions" in error["message"]
    assert "cwd: /work/project" in error["message"]
    assert "stdout neparsované řádky (posledních 20):\nnon-json diagnostic" in error["message"]
    assert "tajny prompt" not in error["message"]


def test_normalize_emits_session_start_once_then_part() -> None:
    client = OpenCodeRunner(config=OpenCodeRunConfig(command="opencode"))

    first = client._normalize(
        {
            "type": "text",
            "sessionID": "ses_1",
            "part": {"id": "prt_1", "messageID": "msg_1", "sessionID": "ses_1", "type": "text", "text": "hello"},
        }
    )
    assert [e.type for e in first] == ["session_start", "part"]
    assert first[0].data == {"session_id": "ses_1"}
    assert client.session_id == "ses_1"

    second = client._normalize(
        {
            "type": "text",
            "sessionID": "ses_1",
            "part": {"id": "prt_2", "messageID": "msg_1", "sessionID": "ses_1", "type": "text", "text": " world"},
        }
    )
    assert [e.type for e in second] == ["part"]


def test_normalize_tool_execute_before_emits_tool_before() -> None:
    client = OpenCodeRunner(config=OpenCodeRunConfig(command="opencode"))
    client.session_id = "ses_1"

    events = client._normalize(
        {
            "source": "opencode-tool-stream",
            "type": "tool.execute.before",
            "sessionID": "ses_1",
            "callID": "call_abc",
            "tool": "bash",
            "input": {"command": "wc -l example.md", "workdir": "/var/www/agentis"},
        }
    )

    assert [e.type for e in events] == ["tool_before"]
    assert events[0].data == {
        "callID": "call_abc",
        "tool": "bash",
        "input": {"command": "wc -l example.md", "workdir": "/var/www/agentis"},
    }


def test_normalize_error_extracts_nested_message() -> None:
    client = OpenCodeRunner(config=OpenCodeRunConfig(command="opencode"))
    client.session_id = "ses_1"

    events = client._normalize(
        {"type": "error", "sessionID": "ses_1", "error": {"name": "APIError", "data": {"message": "no endpoints"}}}
    )

    assert [e.type for e in events] == ["error"]
    assert events[0].data["message"] == "no endpoints"


def test_normalize_step_finish_captures_usage_and_cost() -> None:
    client = OpenCodeRunner(config=OpenCodeRunConfig(command="opencode"))
    client.session_id = "ses_1"

    client._normalize(
        {
            "type": "step_finish",
            "sessionID": "ses_1",
            "part": {
                "id": "prt_x",
                "messageID": "msg_1",
                "type": "step-finish",
                "tokens": {"input": 100, "output": 5, "reasoning": 0, "cache": {"read": 10, "write": 0}},
                "cost": 0.004,
            },
        }
    )

    assert client.last_cost_usd == 0.004
    assert client.last_usage == {
        "input_tokens": 100,
        "output_tokens": 5,
        "reasoning_tokens": 0,
        "cache_read_input_tokens": 10,
        "cache_write_tokens": 0,
    }


def test_stream_parses_json_lines(monkeypatch) -> None:
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": "ses_1",
                "part": {"id": "prt_1", "messageID": "msg_1", "sessionID": "ses_1", "type": "text", "text": "hello"},
            }
        )
        + "\n",
    ]

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(stdout_lines=lines, stderr_lines=[], returncode=0)

    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[OpenCodeEvent]:
        client = OpenCodeRunner(config=OpenCodeRunConfig(command="/usr/bin/opencode"))
        return [event async for event in client.stream("Ahoj")]

    events = asyncio.run(collect_events())
    assert [e.type for e in events] == ["session_start", "part"]


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


@pytest.fixture()
def opencode_client(monkeypatch):
    monkeypatch.setattr("opencode.api.get_settings", lambda: make_settings())
    monkeypatch.setattr(
        OpenCodeAdapterService,
        "create_worktree",
        lambda self: {
            "action": "create_worktree",
            "task_id": self.context.task_id,
            "branch": "task-task-1",
            "base_branch": "master",
            "working_dir": "/srv/worktrees/task-1",
            "status": "created",
        },
    )

    app = create_app()
    return RpcTestClient(app, _DISPATCH), None


def test_health_endpoint(opencode_client):
    client, _manager = opencode_client
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


