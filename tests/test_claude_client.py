from __future__ import annotations

import asyncio
import json
from typing import Any

from claude.client import ClaudeCodeClient, ClaudeRunConfig, KubectlExecTarget


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


class _ReadOnlyChunkStream(_FakeStream):
    async def readline(self) -> bytes:
        raise ValueError("Separator is found, but chunk is longer than limit")


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], stderr_lines: list[str], returncode: int) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


class _HangingStdout:
    """stdout, který vydá jeden řádek a pak už nikdy neuzavře (žádné EOF)."""

    def __init__(self, line: str) -> None:
        self._line = line.encode("utf-8")
        self._emitted = False

    async def read(self, n: int = -1) -> bytes:
        if not self._emitted:
            self._emitted = True
            return self._line
        await asyncio.Event().wait()  # blokuje navždy
        return b""  # pragma: no cover


class _HangingProcess:
    """claude, který po `result` eventu nevyskočí a drží otevřený stdout."""

    def __init__(self, stdout_line: str) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _HangingStdout(stdout_line)
        self.stderr = _FakeStream([])
        self.returncode: int | None = None
        self.pid = 999_999
        self.killed = asyncio.Event()

    async def wait(self) -> int:
        await self.killed.wait()
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.killed.set()


def _raise_process_lookup(_pid: int) -> int:
    raise ProcessLookupError


def test_build_args_includes_dangerous_permissions_for_root(monkeypatch) -> None:
    monkeypatch.setattr("claude.client.os.geteuid", lambda: 0)

    args = ClaudeRunConfig(dangerously_skip_permissions=True).build_args()

    assert "--dangerously-skip-permissions" in args


def test_stream_includes_stderr_in_nonzero_exit_error(monkeypatch) -> None:
    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(stdout_lines=[], stderr_lines=["root failure\n"], returncode=1)

    monkeypatch.setattr("claude.client.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = ClaudeCodeClient(config=ClaudeRunConfig(command="/usr/bin/claude"))
        return [{"type": event.type, **event.data} async for event in client.stream("Ahoj")]

    events = asyncio.run(collect_events())

    assert events[0] == {"type": "stderr", "line": "root failure"}
    assert events[1]["type"] == "error"
    assert events[1]["exit_code"] == 1
    assert events[1]["stderr"] == "root failure"
    assert events[1]["message"] == "claude skončil s kódem 1: root failure"


def test_stream_wraps_local_claude_with_local_setup(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

    monkeypatch.setattr("claude.client.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = ClaudeCodeClient(config=ClaudeRunConfig(command="claude", cwd="/work/project", model="haiku"))
        return [{"type": event.type, **event.data} async for event in client.stream("Ahoj")]

    events = asyncio.run(collect_events())

    assert events == []
    assert captured["args"][:2] == ("bash", "-c")
    assert ". .agentis/local-setup.sh" in captured["args"][2]
    assert "exec claude --print - --output-format stream-json" in captured["args"][2]
    assert "--model haiku" in captured["args"][2]
    assert captured["cwd"] == "/work/project"


def test_stream_uses_informative_stderr_before_kubectl_exit_tail(monkeypatch) -> None:
    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            stdout_lines=[],
            stderr_lines=["claude: unknown option '--bad'\n", "command terminated with exit code 2\n"],
            returncode=2,
        )

    monkeypatch.setattr("claude.client.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = ClaudeCodeClient(config=ClaudeRunConfig(command="/usr/bin/claude"))
        return [{"type": event.type, **event.data} async for event in client.stream("Ahoj")]

    events = asyncio.run(collect_events())

    assert events[-1]["type"] == "error"
    assert events[-1]["exit_code"] == 2
    assert events[-1]["stderr"] == "claude: unknown option '--bad'\ncommand terminated with exit code 2"
    assert events[-1]["message"] == "claude skončil s kódem 2: claude: unknown option '--bad'"


def test_stream_handles_stdout_line_longer_than_asyncio_readline_limit(monkeypatch) -> None:
    long_text = "x" * (70 * 1024)
    long_event = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": long_text}]}}
    )

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
        proc.stdout = _ReadOnlyChunkStream([f"{long_event}\n"])
        return proc

    monkeypatch.setattr("claude.client.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[dict[str, Any]]:
        client = ClaudeCodeClient(config=ClaudeRunConfig(command="/usr/bin/claude"))
        return [{"type": event.type, **event.data} async for event in client.stream("Ahoj")]

    events = asyncio.run(collect_events())

    assert events[0] == {"type": "text", "text": long_text}
    assert events[1]["type"] == "assistant_message"


def test_result_usage_keeps_cache_creation_tokens() -> None:
    client = ClaudeCodeClient(config=ClaudeRunConfig(command="/usr/bin/claude"))

    events = client._normalize(
        {
            "type": "result",
            "session_id": "sess-usage",
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 30,
                "output_tokens": 40,
            },
        }
    )

    assert client.last_usage == {
        "input_tokens": 10,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 30,
        "cache_write_tokens": 0,
        "output_tokens": 40,
    }
    assert events[0].data["usage"] == client.last_usage


def test_stream_stops_and_kills_when_process_hangs_after_result(monkeypatch) -> None:
    # Po `result` claude občas neuzavře stdout ani sám neskončí. Stream se na to
    # nesmí zaseknout — musí doběhnout (a viset zůstavší proces ukončit), jinak
    # se výsledek nikdy nezapíše do Agentisu.
    monkeypatch.setattr("claude.client._PROC_EXIT_GRACE_SEC", 0.05)
    # Lokální mód killuje process group; v testu vynutíme bezpečný fallback na proc.kill().
    monkeypatch.setattr("claude.client.os.getpgid", _raise_process_lookup)

    result_line = json.dumps({"type": "result", "session_id": "s1", "result": "hotovo", "usage": {}}) + "\n"
    proc = _HangingProcess(result_line)

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _HangingProcess:
        return proc

    monkeypatch.setattr("claude.client.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    async def collect_events() -> list[str]:
        client = ClaudeCodeClient(config=ClaudeRunConfig(command="/usr/bin/claude"))
        return [event.type async for event in client.stream("Ahoj")]

    types = asyncio.run(asyncio.wait_for(collect_events(), timeout=5.0))

    assert "result" in types
    assert proc.killed.is_set()


def test_stream_runs_agent_as_kubernetes_job(monkeypatch, tmp_path) -> None:
    from common.kubernetes.agent_job import AgentJobRunner

    captured: dict[str, Any] = {}
    monkeypatch.setattr(AgentJobRunner, "ensure_namespace", lambda self: None)
    monkeypatch.setattr(
        AgentJobRunner, "apply", lambda self, command_script: captured.__setitem__("script", command_script)
    )
    monkeypatch.setattr(AgentJobRunner, "wait_for_pod", lambda self, **kwargs: "pod/agent-run-xyz")
    monkeypatch.setattr(ClaudeCodeClient, "_cleanup_job", lambda self: None)

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

    monkeypatch.setattr("claude.client.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    cwd = tmp_path / "project"
    cwd.mkdir()

    async def collect_events() -> list[dict[str, Any]]:
        client = ClaudeCodeClient(
            config=ClaudeRunConfig(
                command="claude",
                cwd=str(cwd),
                env={"IS_SANDBOX": "1", "AGENTIS_URL": "http://adapter.internal:8002"},
                kubectl_target=KubectlExecTarget(
                    namespace="ns",
                    kubectl="/usr/bin/kubectl",
                    run_manifest_path=str(tmp_path / "run.yaml"),
                    workspace_path=str(cwd),
                ),
            )
        )
        return [{"type": event.type, **event.data} async for event in client.stream("Ahoj")]

    events = asyncio.run(collect_events())

    assert events == []
    assert captured["env"]["IS_SANDBOX"] == "1"
    # The agent CLI is injected into the Job command and reads the prompt file
    # redirected on stdin.
    script = captured["script"]
    assert script.startswith(
        f"cd {cwd} && exec env IS_SANDBOX=1 AGENTIS_URL=http://adapter.internal:8002 "
        "claude --print - --output-format stream-json --verbose "
        "--dangerously-skip-permissions --disallowedTools AskUserQuestion"
    )
    assert script.rstrip().endswith(".md")  # prompt file redirected on stdin
    # The streamed subprocess follows the Job pod logs.
    assert captured["args"] == ("/usr/bin/kubectl", "-n", "ns", "logs", "-f", "pod/agent-run-xyz")
