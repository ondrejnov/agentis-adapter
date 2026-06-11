"""
Async wrapper around the OpenCode (`opencode run <prompt> --format json`).

Spouští `opencode run` jako subprocess pro jedno zadání promptu (bez web REST
API) a čte streamovaný `--format json` výstup po řádcích. Každý řádek je jeden
JSON event ve tvaru::

    {"type": "text", "timestamp": 1779.., "sessionID": "ses_..", "part": {..}}
    {"type": "step_finish", "sessionID": "ses_..", "part": {"type": "step-finish", "tokens": {..}, "cost": 0.4}}
    {"type": "error", "sessionID": "ses_..", "error": {..}}

Eventy se normalizují na ``OpenCodeEvent`` tak, aby šly zpracovat stejnou
session-loop logikou jako Claude Code (viz ``ClaudeSessionManager``):

  - session_start: {session_id}              (jakmile je znám sessionID)
  - part: {part}                             (libovolný OpenCode message Part)
  - tool_before: {callID, tool, input}       (tool.execute.before — nástroj začal běžet)
  - error: {message, error}
  - stderr: {line}
  - raw: {event}                             (nerozpoznaný řádek)

Dlouhé prompty se kvůli limitům délky argv předávají přes dočasný soubor a
krátkou poziční zprávu příkazu ``opencode run``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence

from common.cli_session import unbounded_line_reader as _unbounded_line_reader
from common.workflow.local_env import build_local_env_shell_command


PROMPT_FILE_MESSAGE = "Read the attached prompt file and follow its instructions exactly."
ARG_MAX_FALLBACK = 2 * 1024


# ---------------------------------------------------------------------------
# Eventy
# ---------------------------------------------------------------------------


@dataclass
class OpenCodeEvent:
    """Normalizovaná událost ze streamu `opencode run --format json`."""

    type: str
    data: Dict[str, Any] = field(default_factory=dict)
    raw: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Konfigurace
# ---------------------------------------------------------------------------


@dataclass
class OpenCodeRunConfig:
    command: str = "opencode"
    cwd: Optional[str] = None
    model: Optional[str] = None
    agent: Optional[str] = None
    variant: Optional[str] = None  # provider-specific reasoning effort
    resume_session_id: Optional[str] = None
    dangerously_skip_permissions: bool = True
    extra_args: Sequence[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    timeout_sec: float = 0.0  # 0 = bez limitu

    def build_args(self, message: str, *, prompt_file: Optional[str] = None) -> List[str]:
        args: List[str] = ["run", message]
        if prompt_file:
            args += ["--file", prompt_file]
        args += ["--format", "json"]
        if self.dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")
        if self.resume_session_id:
            args += ["--session", self.resume_session_id]
        if self.model:
            args += ["--model", self.model]
        if self.agent:
            args += ["--agent", self.agent]
        if self.variant:
            args += ["--variant", self.variant]
        if self.extra_args:
            args += list(self.extra_args)
        return args


# ---------------------------------------------------------------------------
# Klient
# ---------------------------------------------------------------------------


class OpenCodeRunner:
    """Tenký asynchronní wrapper na `opencode run`.

    Hlavní metoda ``stream(prompt)`` je async generátor, který emituje
    ``OpenCodeEvent`` objekty postupně, jak chodí ze stdoutu procesu.
    """

    def __init__(self, config: Optional[OpenCodeRunConfig] = None, **kwargs: Any) -> None:
        if config is None:
            config = OpenCodeRunConfig(**kwargs)
        self.config = config
        self.session_id: Optional[str] = None
        self.last_usage: Optional[Dict[str, Any]] = None
        self.last_cost_usd: Optional[float] = None
        self.last_error: Optional[Dict[str, Any]] = None

    @staticmethod
    def _failure_stderr_summary(stderr_lines: list[str], returncode: int) -> str:
        lines = [line.strip() for line in stderr_lines if line.strip()]
        if not lines:
            return ""

        for line in reversed(lines):
            if line not in {"--- Start ---", "--- End ---"}:
                return line
        return lines[-1]

    @staticmethod
    def _safe_command_display(command: str, args: Sequence[str]) -> str:
        display_args = [command, *args]
        if len(display_args) >= 3 and display_args[1] == "run":
            display_args[2] = "<prompt>"
        return shlex.join(display_args)

    @staticmethod
    def _prompt_file_threshold_bytes() -> int:
        return max(1, ARG_MAX_FALLBACK)

    @classmethod
    def _should_use_prompt_file(cls, prompt: str) -> bool:
        return len(prompt.encode("utf-8")) > cls._prompt_file_threshold_bytes()

    @classmethod
    def _failure_message(
        cls,
        cfg: OpenCodeRunConfig,
        args: Sequence[str],
        *,
        returncode: int,
        stderr_lines: list[str],
        stdout_lines: list[str],
    ) -> str:
        message = f"opencode skončil s kódem {returncode}"
        stderr_summary = cls._failure_stderr_summary(stderr_lines, returncode)
        if stderr_summary:
            message = f"{message}: {stderr_summary}"

        details = [f"příkaz: {cls._safe_command_display(cfg.command, args)}"]
        if cfg.cwd:
            details.append(f"cwd: {cfg.cwd}")

        stderr_tail = [line for line in stderr_lines[-20:] if line.strip()]
        if stderr_tail:
            details.append("stderr (posledních 20 řádků):\n" + "\n".join(stderr_tail))

        stdout_tail = [line for line in stdout_lines[-20:] if line.strip()]
        if stdout_tail:
            details.append("stdout neparsované řádky (posledních 20):\n" + "\n".join(stdout_tail))

        if details:
            message = f"{message}\n\nDetaily:\n" + "\n".join(details)
        return message

    async def stream(
        self,
        prompt: str,
        *,
        on_proc_started: Optional[Callable[[asyncio.subprocess.Process], None]] = None,
    ) -> AsyncIterator[OpenCodeEvent]:
        cfg = self.config
        env = {**os.environ, **cfg.env}
        args: List[str]
        prompt_stdin: Optional[bytes] = None
        local_prompt_path: Optional[str] = None
        use_prompt_file = self._should_use_prompt_file(prompt)

        if not shutil.which("bash"):
            yield OpenCodeEvent("error", {"message": "bash nenalezeno v PATH pro lokální spuštění opencode"})
            return

        if use_prompt_file:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".md", prefix="opencode-prompt-", delete=False
            ) as prompt_file:
                prompt_file.write(prompt)
                local_prompt_path = prompt_file.name
            args = cfg.build_args(PROMPT_FILE_MESSAGE, prompt_file=local_prompt_path)
        else:
            args = cfg.build_args(prompt)
        local_command = build_local_env_shell_command([cfg.command, *args], cwd=cfg.cwd)
        print(local_command)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                local_command,
                cwd=cfg.cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except BaseException:
            if local_prompt_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(local_prompt_path)
            raise

        if on_proc_started is not None:
            try:
                on_proc_started(proc)
            except Exception:
                pass

        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        try:
            if prompt_stdin is not None:
                proc.stdin.write(prompt_stdin)
                await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            pass

        stderr_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        stderr_lines: List[str] = []
        stdout_lines: List[str] = []

        async def _pump_stderr() -> None:
            assert proc.stderr is not None
            read_stderr_line = _unbounded_line_reader(proc.stderr)
            while True:
                line = await read_stderr_line()
                if not line:
                    await stderr_queue.put(None)
                    return
                await stderr_queue.put(line.decode("utf-8", errors="replace").rstrip("\n"))

        stderr_task = asyncio.create_task(_pump_stderr())

        async def _drain_stderr_nonblocking() -> List[OpenCodeEvent]:
            out: List[OpenCodeEvent] = []
            while True:
                try:
                    item = stderr_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return out
                if item is None:
                    return out
                if item.strip():
                    stderr_lines.append(item)
                    out.append(OpenCodeEvent("stderr", {"line": item}))

        produced_error = False
        try:
            timeout = cfg.timeout_sec if cfg.timeout_sec and cfg.timeout_sec > 0 else None
            deadline = (asyncio.get_event_loop().time() + timeout) if timeout else None
            read_stdout_line = _unbounded_line_reader(proc.stdout)

            while True:
                for ev in await _drain_stderr_nonblocking():
                    yield ev

                read_coro = read_stdout_line()
                if deadline is not None:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        proc.kill()
                        yield OpenCodeEvent("error", {"message": f"timeout po {timeout}s"})
                        return
                    line_bytes = await asyncio.wait_for(read_coro, timeout=remaining)
                else:
                    line_bytes = await read_coro

                if not line_bytes:
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    stdout_lines.append(line)
                    yield OpenCodeEvent("raw", {"line": line})
                    continue

                for normalized in self._normalize(event):
                    if normalized.type == "error":
                        produced_error = True
                    yield normalized
        finally:
            try:
                await proc.wait()
            except Exception:
                pass
            with __import__("contextlib").suppress(BaseException):
                await stderr_task

            tail: List[str] = []
            while not stderr_queue.empty():
                item = stderr_queue.get_nowait()
                if item:
                    stderr_lines.append(item)
                    tail.append(item)
            if local_prompt_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(local_prompt_path)
            for line in tail:
                yield OpenCodeEvent("stderr", {"line": line})

            if proc.returncode and proc.returncode != 0 and not produced_error:
                message = self._failure_message(
                    cfg,
                    args,
                    returncode=proc.returncode,
                    stderr_lines=stderr_lines,
                    stdout_lines=stdout_lines,
                )
                yield OpenCodeEvent(
                    "error",
                    {
                        "message": message,
                        "exit_code": proc.returncode,
                        "stderr": "\n".join(stderr_lines[-20:]),
                        "stdout": "\n".join(stdout_lines[-20:]),
                    },
                )

    # -- normalizace -------------------------------------------------------

    def _normalize(self, event: Dict[str, Any]) -> List[OpenCodeEvent]:
        out: List[OpenCodeEvent] = []
        if not isinstance(event, dict):
            return [OpenCodeEvent("raw", {"event": event})]

        session_id = event.get("sessionID") or event.get("session_id")
        if isinstance(session_id, str) and session_id and session_id != self.session_id:
            self.session_id = session_id
            out.append(OpenCodeEvent("session_start", {"session_id": session_id}, raw=event))

        etype = event.get("type")

        if etype == "error":
            error = event.get("error") or {}
            message = self._error_message(error) or "OpenCode selhal"
            self.last_error = error if isinstance(error, dict) else {"message": message}
            out.append(OpenCodeEvent("error", {"message": message, "error": error}, raw=event))
            return out

        if etype == "tool.execute.before":
            # `tool.execute.before` nemá `part` ani `messageID` — nese jen callID,
            # název nástroje a vstup. Předáme je dál jako samostatný event, ze
            # kterého mapper složí běžící (status=running) tool part.
            out.append(
                OpenCodeEvent(
                    "tool_before",
                    {
                        "callID": event.get("callID"),
                        "tool": event.get("tool"),
                        "input": event.get("input"),
                    },
                    raw=event,
                )
            )
            return out

        part = event.get("part")
        if isinstance(part, dict):
            if part.get("type") == "step-finish":
                self._capture_usage(part)
            out.append(OpenCodeEvent("part", {"part": part}, raw=event))
            return out

        out.append(OpenCodeEvent("raw", {"event": event}, raw=event))
        return out

    @staticmethod
    def _error_message(error: Any) -> str:
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            data = error.get("data")
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                return data["message"]
            for key in ("message", "name"):
                if isinstance(error.get(key), str) and error[key]:
                    return error[key]
        return ""

    def _capture_usage(self, part: Dict[str, Any]) -> None:
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            cache_value = tokens.get("cache")
            cache: Dict[str, Any] = cache_value if isinstance(cache_value, dict) else {}
            self.last_usage = {
                "input_tokens": int(tokens.get("input") or 0),
                "output_tokens": int(tokens.get("output") or 0),
                "reasoning_tokens": int(tokens.get("reasoning") or 0),
                "cache_read_input_tokens": int(cache.get("read") or 0),
                "cache_write_tokens": int(cache.get("write") or 0),
            }
        cost = part.get("cost")
        if isinstance(cost, (int, float)):
            self.last_cost_usd = float(cost)
