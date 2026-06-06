from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from common.config import Settings
from common.cli_session import KubectlExecTarget
from common.models import AdapterOptionsPayload, AgentExecutionContextPayload
from opencode.api import create_app, _DISPATCH
from tests.support import RpcTestClient
from opencode.adapter import OpenCodeAdapterService
from opencode.runner import OpenCodeRunner, OpenCodeEvent, OpenCodeRunConfig
from opencode.activity_mapper import OpenCodeActivityMapper
from opencode.session_manager import OpenCodeSessionManager


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8003,
        "default_namespace": "agentis",
        "app_host": None,
        "manifest_path": Path("/tmp/opencode.yaml"),
        "worktree_root": Path("/var/www/worktrees"),
        "public_base_url": "http://adapter.internal:8003",
        "agentis_endpoint": None,
        "agentis_token": None,
        "claude_run_mode": "local",
        "claude_pod_selector": "deployment/opencode",
        "claude_pod_container": "opencode",
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


def test_stream_wraps_local_opencode_with_local_setup(monkeypatch) -> None:
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
    assert ". .agentis/local-setup.sh" in captured["args"][2]
    assert "exec opencode run 'Do X' --format json" in captured["args"][2]
    assert "--file" not in captured["args"][2]
    assert "--model haiku" in captured["args"][2]
    assert captured["cwd"] == "/work/project"


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


def test_stream_passes_config_env_into_kubectl_exec_shell(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    process = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return process

    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = OpenCodeRunner(
            config=OpenCodeRunConfig(
                command="opencode",
                cwd="/work/project",
                env={"IS_SANDBOX": "1"},
                kubectl_target=KubectlExecTarget(
                    namespace="ns", selector="deployment/opencode", kubectl="/usr/bin/kubectl"
                ),
            )
        )
        return [{"type": event.type, **event.data} async for event in client.stream("Ahoj")]

    events = asyncio.run(collect_events())

    assert events == []
    assert captured["env"]["IS_SANDBOX"] == "1"
    assert captured["args"][-2:] == (
        "-c",
        "cd /work/project && exec env IS_SANDBOX=1 opencode run Ahoj --format json --dangerously-skip-permissions",
    )
    assert process.stdin.data == bytearray()
    assert process.stdin.closed is True


def test_stream_uses_pod_temp_prompt_file_for_long_kubectl_prompt(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    process = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

    monkeypatch.setattr(OpenCodeRunner, "_prompt_file_threshold_bytes", staticmethod(lambda: 10))

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return process

    monkeypatch.setattr("opencode.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = OpenCodeRunner(
            config=OpenCodeRunConfig(
                command="opencode",
                cwd="/work/project",
                env={"IS_SANDBOX": "1"},
                kubectl_target=KubectlExecTarget(
                    namespace="ns", selector="deployment/opencode", kubectl="/usr/bin/kubectl"
                ),
            )
        )
        return [{"type": event.type, **event.data} async for event in client.stream("x" * 11)]

    events = asyncio.run(collect_events())

    assert events == []
    assert captured["env"]["IS_SANDBOX"] == "1"
    assert captured["args"][-2] == "-c"
    shell = captured["args"][-1]
    assert shell.startswith("tmp=/tmp/opencode-prompt-")
    assert 'trap \'rm -f "$tmp"\' EXIT; cat > "$tmp";' in shell
    assert "cd /work/project && env IS_SANDBOX=1 opencode run" in shell
    assert "--file /tmp/opencode-prompt-" in shell
    assert "--format json --dangerously-skip-permissions" in shell
    assert "x" * 11 not in shell
    assert process.stdin.data.decode("utf-8") == "x" * 11
    assert process.stdin.closed is True


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
# OpenCodeActivityMapper
# ---------------------------------------------------------------------------


def test_mapper_seeds_user_prompt() -> None:
    mapper = OpenCodeActivityMapper(prompt="Popis ukolu")
    snapshot = mapper.snapshot()
    assert snapshot[0]["info"]["role"] == "user"
    assert UUID(snapshot[0]["info"]["id"]).version == 7
    assert UUID(snapshot[0]["parts"][0]["id"]).version == 7
    assert snapshot[0]["parts"][0]["messageID"] == snapshot[0]["info"]["id"]
    assert snapshot[0]["parts"][0]["text"] == "Popis ukolu"


def test_mapper_session_start_propagates_session_id() -> None:
    mapper = OpenCodeActivityMapper(prompt="x")
    changed = mapper.consume(OpenCodeEvent("session_start", {"session_id": "ses_1"}))
    assert changed is True
    assert mapper.session_id == "ses_1"
    assert mapper.snapshot()[0]["info"]["sessionID"] == "ses_1"


def test_mapper_builds_assistant_message_from_parts() -> None:
    mapper = OpenCodeActivityMapper(prompt="x")
    mapper.consume(OpenCodeEvent("session_start", {"session_id": "ses_1"}))

    text_part = {"id": "prt_1", "messageID": "msg_1", "sessionID": "ses_1", "type": "text", "text": "hi"}
    assert mapper.consume(OpenCodeEvent("part", {"part": text_part})) is True

    # Same part id updates in place; new part id appends.
    updated = {"id": "prt_1", "messageID": "msg_1", "sessionID": "ses_1", "type": "text", "text": "hi there"}
    mapper.consume(OpenCodeEvent("part", {"part": updated}))

    snapshot = mapper.snapshot()
    assert len(snapshot) == 2
    assistant = snapshot[1]
    assert assistant["info"]["role"] == "assistant"
    assert UUID(assistant["info"]["id"]).version == 7
    assert len(assistant["parts"]) == 1
    assert UUID(assistant["parts"][0]["id"]).version == 7
    assert assistant["parts"][0]["id"] != "prt_1"
    assert assistant["parts"][0]["messageID"] == assistant["info"]["id"]
    assert assistant["parts"][0]["text"] == "hi there"


def test_mapper_tool_before_emits_running_then_completed_updates_in_place() -> None:
    mapper = OpenCodeActivityMapper(prompt="x")
    mapper.consume(OpenCodeEvent("session_start", {"session_id": "ses_1"}))

    # step-start fixuje aktuální assistant zprávu, na kterou se běžící tool naváže.
    step_start = {"id": "prt_s", "messageID": "msg_1", "sessionID": "ses_1", "type": "step-start"}
    mapper.consume(OpenCodeEvent("part", {"part": step_start}))

    before = {
        "callID": "call_abc",
        "tool": "bash",
        "input": {"command": "wc -l example.md"},
    }
    assert mapper.consume(OpenCodeEvent("tool_before", before)) is True

    snapshot = mapper.snapshot()
    assistant = snapshot[1]
    tool_parts = [p for p in assistant["parts"] if p["type"] == "tool"]
    assert len(tool_parts) == 1
    running = tool_parts[0]
    assert running["state"]["status"] == "running"
    assert running["callID"] == "call_abc"
    assert running["state"]["input"] == {"command": "wc -l example.md"}
    assert UUID(running["id"]).version == 7
    running_id = running["id"]

    # Dokončený tool part nese vlastní part id, ale stejné callID — musí
    # přepsat běžící placeholder, ne přidat druhý.
    completed = {
        "id": "prt_tool",
        "messageID": "msg_1",
        "sessionID": "ses_1",
        "type": "tool",
        "tool": "bash",
        "callID": "call_abc",
        "state": {"status": "completed", "input": {"command": "wc -l example.md"}, "output": "101\n"},
    }
    mapper.consume(OpenCodeEvent("part", {"part": completed}))

    snapshot = mapper.snapshot()
    assistant = snapshot[1]
    tool_parts = [p for p in assistant["parts"] if p["type"] == "tool"]
    assert len(tool_parts) == 1
    done = tool_parts[0]
    assert done["state"]["status"] == "completed"
    assert done["state"]["output"] == "101\n"
    # Veřejné part id zůstává stabilní napříč running -> completed.
    assert done["id"] == running_id


def test_mapper_tool_before_without_message_is_ignored() -> None:
    mapper = OpenCodeActivityMapper(prompt="x")
    mapper.consume(OpenCodeEvent("session_start", {"session_id": "ses_1"}))

    before = {"callID": "call_abc", "tool": "bash", "input": {}}
    assert mapper.consume(OpenCodeEvent("tool_before", before)) is False
    assert len(mapper.snapshot()) == 1  # jen úvodní user zpráva


def test_mapper_step_finish_updates_tokens_and_cost() -> None:
    mapper = OpenCodeActivityMapper(prompt="x")
    mapper.consume(OpenCodeEvent("session_start", {"session_id": "ses_1"}))
    step = {
        "id": "prt_f",
        "messageID": "msg_1",
        "type": "step-finish",
        "reason": "stop",
        "tokens": {"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 1, "write": 0}},
        "cost": 0.01,
    }
    mapper.consume(OpenCodeEvent("part", {"part": step}))

    info = mapper.snapshot()[1]["info"]
    assert info["cost"] == 0.01
    assert info["finish"] == "stop"
    assert info["tokens"] == {"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 1, "write": 0}}


# ---------------------------------------------------------------------------
# OpenCodeAdapterService
# ---------------------------------------------------------------------------


def test_deploy_is_skipped_for_local_opencode() -> None:
    adapter = OpenCodeAdapterService(
        context=make_context(),
        settings=make_settings(),
        session_manager=MagicMock(spec=OpenCodeSessionManager),
    )
    assert adapter.deploy() == {
        "action": "deploy",
        "task_id": "task-1",
        "status": "skipped",
        "reason": "opencode_local",
    }


def test_wait_ready_returns_local_url() -> None:
    adapter = OpenCodeAdapterService(
        context=make_context(),
        settings=make_settings(),
        session_manager=MagicMock(spec=OpenCodeSessionManager),
    )
    assert adapter.wait_ready() == {
        "action": "wait_ready",
        "task_id": "task-1",
        "url": "local://opencode",
        "status": "skipped",
    }


def test_claude_run_mode_kubernetes_does_not_leak_into_opencode() -> None:
    # OpenCode must default to local even when claude_run_mode is kubernetes.
    adapter = OpenCodeAdapterService(
        context=make_context(),
        settings=make_settings(claude_run_mode="kubernetes"),
        session_manager=MagicMock(spec=OpenCodeSessionManager),
    )
    assert adapter.is_kubernetes_mode is False
    assert adapter.deploy()["status"] == "skipped"


def test_context_runtime_kubernetes_enables_kubernetes_mode() -> None:
    adapter = OpenCodeAdapterService(
        context=make_context(adapter=AdapterOptionsPayload(runtime="kubernetes", agent="build")),
        settings=make_settings(),
        session_manager=MagicMock(spec=OpenCodeSessionManager),
    )
    assert adapter.is_kubernetes_mode is True


def test_ci_setup_steps_skipped_in_local_mode() -> None:
    adapter = OpenCodeAdapterService(
        context=make_context(),
        settings=make_settings(),
        session_manager=MagicMock(spec=OpenCodeSessionManager),
    )
    assert adapter.is_kubernetes_mode is False
    assert adapter.ci_setup_steps() == []


def test_ci_setup_steps_delegates_to_runtime_in_kubernetes_mode(monkeypatch) -> None:
    from common.kubernetes.ci_workflow import CiStep

    adapter = OpenCodeAdapterService(
        context=make_context(adapter=AdapterOptionsPayload(runtime="kubernetes", agent="build")),
        settings=make_settings(),
        session_manager=MagicMock(spec=OpenCodeSessionManager),
    )
    steps = [CiStep(id="1-venv", name="Create venv", run="python -m venv .venv")]
    monkeypatch.setattr(
        "common.kubernetes.runtime.KubernetesRuntime.ci_setup_steps",
        lambda self: steps,
    )

    assert adapter.ci_setup_steps() == steps


def test_start_session_starts_session_manager(monkeypatch) -> None:
    manager = MagicMock(spec=OpenCodeSessionManager)
    manager.start.return_value = "ses_abc"
    manager.get_snapshot_key.return_value = "snap-start"
    monkeypatch.setattr(OpenCodeAdapterService, "_persist_agentis_session_id", lambda self, session_id: None)

    context = make_context()
    adapter = OpenCodeAdapterService(
        context=context,
        settings=make_settings(worktree_root=Path("/srv/worktrees")),
        session_manager=manager,
    )

    result = adapter.start_session(pod_url="local://opencode")

    assert result == {
        "action": "start_session",
        "task_id": "task-1",
        "session_id": "ses_abc",
        "snapshot_key": "snap-start",
    }
    kwargs = manager.start.call_args.kwargs
    assert kwargs["worktree"] == "/srv/worktrees/task-1"
    assert kwargs["prompt"] == "Popis ukolu"
    assert "kubectl_target" not in kwargs
    assert context.session_id == "ses_abc"


def test_add_message_forwards_to_session_manager() -> None:
    manager = MagicMock(spec=OpenCodeSessionManager)
    manager.get_snapshot_key.return_value = "snap-send"
    context = make_context(session_id="ses_abc")
    adapter = OpenCodeAdapterService(
        context=context,
        settings=make_settings(worktree_root=Path("/srv/worktrees")),
        session_manager=manager,
    )

    result = adapter.add_message("ahoj")

    assert result == {
        "action": "add_message",
        "task_id": "task-1",
        "session_id": "ses_abc",
        "snapshot_key": "snap-send",
    }
    manager.send.assert_called_once_with(
        session_id="ses_abc",
        context=context,
        worktree="/srv/worktrees/task-1",
        prompt="ahoj",
    )


def test_abort_delegates_to_session_manager() -> None:
    manager = MagicMock(spec=OpenCodeSessionManager)
    adapter = OpenCodeAdapterService(
        context=make_context(),
        settings=make_settings(),
        session_manager=manager,
    )
    assert adapter.abort("ses_abc") == {"action": "abort", "task_id": "task-1", "session_id": "ses_abc"}
    manager.abort.assert_called_once_with("ses_abc")


def test_session_manager_uses_opencode_labels_and_pkill_pattern() -> None:
    manager = OpenCodeSessionManager(settings=make_settings())
    assert manager._AGENT_LABEL == "opencode"
    assert manager._REMOTE_PKILL_PATTERN == "opencode run"


# ---------------------------------------------------------------------------
# JSON-RPC integration via the FastAPI app
# ---------------------------------------------------------------------------


@pytest.fixture()
def opencode_client(monkeypatch):
    fake_manager = MagicMock(spec=OpenCodeSessionManager)
    fake_manager.start.return_value = "ses_abc123"

    monkeypatch.setattr("opencode.api.OpenCodeSessionManager", lambda settings: fake_manager)
    monkeypatch.setattr("opencode.api.get_settings", lambda: make_settings())
    monkeypatch.setattr(OpenCodeAdapterService, "_persist_agentis_session_id", lambda self, session_id: None)
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
    return RpcTestClient(app, _DISPATCH), fake_manager


def test_health_endpoint(opencode_client):
    client, _manager = opencode_client
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_start_dispatches_to_opencode_session_manager(opencode_client):
    client, manager = opencode_client

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "start",
            "params": {
                "context": {
                    "run_id": "run-1",
                    "task_id": "task-1",
                    "title": "Implementace nove funkce",
                    "description": "Popis ukolu",
                    "project_slug": "agentis",
                    "working_dir": "/var/www/repo",
                    "adapter": {"agent": "build", "model": "openrouter/openai/gpt-4.1-mini"},
                }
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()["result"]
    steps = [step["action"] for step in payload["adapter"]["steps"]]
    assert steps == ["create_worktree", "deploy", "wait_ready", "start_session"]
    assert payload["adapter"]["steps"][1]["status"] == "skipped"
    assert payload["adapter"]["steps"][2]["url"] == "local://opencode"
    assert payload["adapter"]["steps"][3]["session_id"] == "ses_abc123"
    manager.start.assert_called_once()
