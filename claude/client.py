"""
Async wrapper around the Claude Code CLI (`claude --print --output-format stream-json --verbose`).

Spouští `claude` jako subprocess, čte stream-json výstup po řádcích a normalizuje
ho na události, které lze postupně streamovat do webové aplikace (SSE / WebSocket).

Inspirováno wrapperem v paperclip/claude-local (execute.ts + parse.ts), ale
napsáno tak, aby šlo přímo přihazovat eventy do FastAPI streamu místo čekání
na finální výsledek.

Použití:

    client = ClaudeCodeClient(cwd="/work/project")
    async for event in client.stream(prompt="udelej X"):
        print(event.type, event.data)
        # event.type ∈ {session_start, text, tool_use, tool_result,
        #               assistant_message, result, stderr, error}

Pro pohodlnější použití jsou k dispozici i typed dataclassy a helper
`run_collect()` pro testovací režim.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence

import asyncio

from common.cli_session import unbounded_line_reader as _unbounded_line_reader
from common.workflow.local_env import build_local_env_shell_command


# Po terminálním `result` eventu necháme claude CLI doběhnout jen krátce; pokud
# stdout neuzavře a sám neskončí, tvrdě ho ukončíme.
_PROC_EXIT_GRACE_SEC = 10.0


# ---------------------------------------------------------------------------
# Eventy
# ---------------------------------------------------------------------------


@dataclass
class ClaudeEvent:
    """Normalizovaná událost ze streamu Claude Code CLI."""

    type: str
    data: Dict[str, Any] = field(default_factory=dict)
    raw: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        payload = {"type": self.type, **self.data}
        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Konfigurace
# ---------------------------------------------------------------------------


@dataclass
class ClaudeRunConfig:
    command: str = "claude"
    cwd: Optional[str] = None
    model: Optional[str] = None
    agent: Optional[str] = None
    effort: Optional[str] = None  # např. "low" | "medium" | "high"
    max_turns: Optional[int] = None
    dangerously_skip_permissions: bool = True
    chrome: bool = False
    resume_session_id: Optional[str] = None
    append_system_prompt_file: Optional[str] = None
    add_dirs: Sequence[str] = field(default_factory=list)
    extra_args: Sequence[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    timeout_sec: float = 0.0  # 0 = bez limitu

    def build_args(self) -> List[str]:
        if self.command != "claude-p":
            args = ["--print", "-"]
        else:
            args = []

        args.extend(
            [
                "--output-format",
                "stream-json",
                "--verbose",
                "--dangerously-skip-permissions",
                "--disallowedTools",
                "AskUserQuestion",
            ]
        )
        if self.resume_session_id:
            args += ["--resume", self.resume_session_id]
        if self.chrome:
            args.append("--chrome")
        if self.model:
            args += ["--model", self.model]
        if self.agent:
            args += ["--agent", self.agent]
        if self.effort:
            args += ["--effort", self.effort]
        if self.max_turns and self.max_turns > 0:
            args += ["--max-turns", str(self.max_turns)]
        if self.append_system_prompt_file and not self.resume_session_id:
            args += ["--append-system-prompt-file", self.append_system_prompt_file]
        for d in self.add_dirs:
            args += ["--add-dir", d]
        if self.extra_args:
            args += list(self.extra_args)
        return args


# ---------------------------------------------------------------------------
# Klient
# ---------------------------------------------------------------------------


class ClaudeCodeError(RuntimeError):
    def __init__(self, message: str, *, exit_code: Optional[int] = None, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class ClaudeCodeClient:
    """
    Tenký asynchronní wrapper na CLI `claude`.

    Hlavní metoda `stream(prompt)` je async generátor, který emituje
    `ClaudeEvent` objekty postupně, jak chodí ze stdoutu CLI. Konzument
    je může okamžitě posílat klientovi přes SSE/WebSocket.
    """

    def __init__(self, config: Optional[ClaudeRunConfig] = None, **kwargs: Any) -> None:
        if config is None:
            config = ClaudeRunConfig(**kwargs)
        self.config = config
        # Souhrnný stav z posledního běhu — vyplní se postupně během streamu.
        self._session_started = False
        self.session_id: Optional[str] = None
        self.model: Optional[str] = None
        self.last_result: Optional[Dict[str, Any]] = None
        self.last_usage: Optional[Dict[str, Any]] = None
        self.last_cost_usd: Optional[float] = None

    @staticmethod
    def _failure_stderr_summary(stderr_lines: list[str], returncode: int) -> str:
        lines = [line.strip() for line in stderr_lines if line.strip()]
        if not lines:
            return ""
        return lines[-1]

    async def _terminate_proc(self, proc: asyncio.subprocess.Process) -> None:
        """Tvrdě ukončí běžící claude proces (a jeho potomky)."""
        if proc.returncode is not None:
            return
        # Proces má vlastní session/process group (start_new_session),
        # killneme celou skupinu, ať jdou dolů i potomci claude CLI.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, OSError):
            pass
        with contextlib.suppress(Exception):
            proc.kill()

    # -- veřejné API -------------------------------------------------------

    async def stream(
        self,
        prompt: str,
        *,
        on_proc_started: Optional[Callable[[asyncio.subprocess.Process], None]] = None,
    ) -> AsyncIterator[ClaudeEvent]:
        """
        Spustí Claude CLI, pošle prompt na stdin a yielduje normalizované eventy.

        Eventy:
          - session_start: {session_id, model, tools?, cwd?}
          - text: {text}                         (assistant text delta/blok)
          - tool_use: {id, name, input}
          - tool_result: {tool_use_id, content, is_error}
          - assistant_message: {message}         (kompletní zpráva)
          - thinking: {text}
          - result: {summary, usage, cost_usd, session_id, is_error, subtype}
          - stderr: {line}
          - error: {message}

        ``on_proc_started`` je volitelný callback, který je zavolán hned po
        vytvoření subprocesu. Slouží k tomu, aby si ho volající mohl uložit
        a později ho v případě potřeby zabít (abort).
        """
        cfg = self.config
        env = {**os.environ, **cfg.env}
        args = cfg.build_args()

        if not shutil.which("bash"):
            yield ClaudeEvent("error", {"message": "bash nenalezeno v PATH pro lokální spuštění claude"})
            return

        local_command = build_local_env_shell_command([cfg.command, *args], cwd=cfg.cwd)
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

        if on_proc_started is not None:
            try:
                on_proc_started(proc)
            except Exception:
                pass

        # Pošleme prompt na stdin a uzavřeme ho, aby CLI věděl, že je hotovo.
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

        stderr_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        stderr_lines: List[str] = []

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

        async def _drain_stderr_nonblocking() -> List[ClaudeEvent]:
            out: List[ClaudeEvent] = []
            while True:
                try:
                    item = stderr_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return out
                if item is None:
                    return out
                if item.strip():
                    stderr_lines.append(item)
                    out.append(ClaudeEvent("stderr", {"line": item}))

        try:
            timeout = cfg.timeout_sec if cfg.timeout_sec and cfg.timeout_sec > 0 else None
            deadline = (asyncio.get_event_loop().time() + timeout) if timeout else None
            read_stdout_line = _unbounded_line_reader(proc.stdout)

            while True:
                # Před každým readem odeslat to, co se případně nakupilo na stderru.
                for ev in await _drain_stderr_nonblocking():
                    yield ev

                read_coro = read_stdout_line()
                if deadline is not None:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        proc.kill()
                        yield ClaudeEvent("error", {"message": f"timeout po {timeout}s"})
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
                    # CLI nepoužívá vždy čistý stream-json — emituj jako raw.
                    yield ClaudeEvent("raw", {"line": line})
                    continue

                terminal = False
                for normalized in self._normalize(event):
                    yield normalized
                    # `result` je poslední event běhu. Po něm už nic
                    # smysluplného nechodí; nečekáme na EOF stdoutu, protože
                    # claude ho někdy neuzavře.
                    if normalized.type == "result":
                        terminal = True
                if terminal:
                    break
        finally:
            # Doběh stderru a procesu. Na ukončení procesu čekáme jen krátce;
            # když po `result` eventu nevyskočí sám, ukončíme ho, ať nezůstane
            # viset a generátor může doběhnout.
            try:
                await asyncio.wait_for(proc.wait(), timeout=_PROC_EXIT_GRACE_SEC)
            except asyncio.TimeoutError:
                await self._terminate_proc(proc)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=_PROC_EXIT_GRACE_SEC)
            except Exception:
                pass
            with contextlib.suppress(BaseException):
                await stderr_task

            # Zbytek stderru.
            tail: List[str] = []
            while not stderr_queue.empty():
                item = stderr_queue.get_nowait()
                if item:
                    stderr_lines.append(item)
                    tail.append(item)
            for line in tail:
                yield ClaudeEvent("stderr", {"line": line})

            if proc.returncode and proc.returncode != 0 and self.last_result is None:
                message = f"claude skončil s kódem {proc.returncode}"
                stderr_summary = self._failure_stderr_summary(stderr_lines, proc.returncode)
                if stderr_summary:
                    message = f"{message}: {stderr_summary}"
                yield ClaudeEvent(
                    "error",
                    {
                        "message": message,
                        "exit_code": proc.returncode,
                        "stderr": "\n".join(stderr_lines[-20:]),
                    },
                )

    async def run_collect(self, prompt: str) -> Dict[str, Any]:
        """
        Pohodlný režim pro testy / synchronní použití: spotřebuje celý stream
        a vrátí souhrn (events, summary, usage, session_id, cost).
        """
        events: List[ClaudeEvent] = []
        summary_parts: List[str] = []
        async for ev in self.stream(prompt):
            events.append(ev)
            if ev.type == "text":
                summary_parts.append(ev.data.get("text", ""))
        return {
            "events": [{"type": e.type, **e.data} for e in events],
            "summary": (self.last_result or {}).get("result") or "\n\n".join(summary_parts).strip(),
            "session_id": self.session_id,
            "model": self.model,
            "usage": self.last_usage,
            "cost_usd": self.last_cost_usd,
            "result": self.last_result,
        }

    # -- normalizace -------------------------------------------------------

    def _normalize(self, event: Dict[str, Any]) -> List[ClaudeEvent]:
        """Převede jeden řádek stream-json na 0-N normalizovaných eventů."""
        out: List[ClaudeEvent] = []
        etype = event.get("type")

        if etype == "system" and event.get("subtype") == "init":
            self.session_id = event.get("session_id") or self.session_id
            self.model = event.get("model") or self.model
            self._session_started = True
            out.append(
                ClaudeEvent(
                    "session_start",
                    {
                        "session_id": self.session_id,
                        "model": self.model,
                        "tools": event.get("tools"),
                        "cwd": event.get("cwd"),
                        "mcp_servers": event.get("mcp_servers"),
                    },
                    raw=event,
                )
            )
            return out

        # claude-p neposílá `system/init`; jeho úvodní událost je `mode` a
        # session id nese v camelCase klíči `sessionId` (snake_case `session_id`
        # má až finální `result`). Odvoď proto `session_start` z prvního eventu,
        # který nese session id, ať telemetrie/konzumenti dostanou session včas.
        session_id = event.get("session_id") or event.get("sessionId")
        if not self._session_started and etype != "result" and isinstance(session_id, str) and session_id:
            self.session_id = session_id
            self.model = (event.get("message") or {}).get("model") or event.get("model") or self.model
            self._session_started = True
            out.append(
                ClaudeEvent(
                    "session_start",
                    {"session_id": self.session_id, "model": self.model, "cwd": event.get("cwd")},
                    raw=event,
                )
            )
            # `mode`/`permission-mode`/`file-history-snapshot` nemají další obsah
            # k normalizaci; `assistant`/`user` necháme propadnout do svých
            # handlerů níž, ať se jejich obsah nezahodí. `result` má vlastní
            # session id i handler, takže ho jako session-start signál nebereme.
            if etype not in ("assistant", "user"):
                return out

        if etype == "assistant":
            self.session_id = event.get("session_id") or self.session_id
            message = event.get("message") or {}
            content = message.get("content") or []
            # Claude rozkládá jednu assistant zprávu (stejné `message.id`) do více
            # stream-json řádků a na každém opakuje identický `usage`. Protáhneme
            # id do všech dílčích eventů, ať je mapper umí složit do jedné zprávy
            # a usage započítat jen jednou.
            msg_id = message.get("id")

            def _with_msg_id(data: Dict[str, Any]) -> Dict[str, Any]:
                if msg_id:
                    data["message_id"] = msg_id
                return data

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if text:
                        out.append(ClaudeEvent("text", _with_msg_id({"text": text}), raw=event))
                elif btype == "tool_use":
                    out.append(
                        ClaudeEvent(
                            "tool_use",
                            _with_msg_id(
                                {
                                    "id": block.get("id"),
                                    "name": block.get("name"),
                                    "input": block.get("input"),
                                }
                            ),
                            raw=event,
                        )
                    )
                elif btype == "thinking":
                    out.append(ClaudeEvent("thinking", _with_msg_id({"text": block.get("thinking") or ""}), raw=event))
                else:
                    out.append(ClaudeEvent("assistant_block", _with_msg_id({"block": block}), raw=event))
            # Doplníme i kompletní message pro klienty, co chtějí surovou zprávu.
            out.append(ClaudeEvent("assistant_message", _with_msg_id({"message": message}), raw=event))
            return out

        if etype == "user":
            # `user` zprávy v stream-json obvykle obsahují tool_result bloky.
            message = event.get("message") or {}
            content = message.get("content") or []
            emitted = False
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    out.append(
                        ClaudeEvent(
                            "tool_result",
                            {
                                "tool_use_id": block.get("tool_use_id"),
                                "content": block.get("content"),
                                "is_error": bool(block.get("is_error", False)),
                            },
                            raw=event,
                        )
                    )
                    emitted = True
            if not emitted:
                out.append(ClaudeEvent("user_message", {"message": message}, raw=event))
            return out

        if etype == "result":
            self.session_id = event.get("session_id") or self.session_id
            usage = event.get("usage") or {}
            self.last_usage = {
                "input_tokens": usage.get("input_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "cache_write_tokens": usage.get("cache_write_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
            cost = event.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                self.last_cost_usd = float(cost)
            self.last_result = event
            out.append(
                ClaudeEvent(
                    "result",
                    {
                        "summary": event.get("result"),
                        "usage": self.last_usage,
                        "cost_usd": self.last_cost_usd,
                        "session_id": self.session_id,
                        "is_error": bool(event.get("is_error", False)),
                        "subtype": event.get("subtype"),
                    },
                    raw=event,
                )
            )
            return out

        # Neznámý typ — předáme dál jako raw, aby se neztratil.
        out.append(ClaudeEvent("raw", {"event_type": etype, "event": event}, raw=event))
        return out


# ---------------------------------------------------------------------------
# Pomocníci pro FastAPI SSE
# ---------------------------------------------------------------------------


async def stream_as_sse(client: ClaudeCodeClient, prompt: str) -> AsyncIterator[bytes]:
    """
    Adaptér pro FastAPI `StreamingResponse(media_type="text/event-stream")`.

    Příklad:

        from fastapi.responses import StreamingResponse

        @app.post("/claude/stream")
        async def claude_stream(req: PromptRequest):
            client = ClaudeCodeClient(cwd=req.cwd, model=req.model)
            return StreamingResponse(
                stream_as_sse(client, req.prompt),
                media_type="text/event-stream",
            )
    """
    async for event in client.stream(prompt):
        payload = event.to_json()
        yield f"event: {event.type}\ndata: {payload}\n\n".encode("utf-8")
