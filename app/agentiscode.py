"""``agentiscode`` — commandline wrapper nad OpenCode a Claude Code.

Jeden vstupní bod, který podle ``--adapter`` spustí buď ``opencode run`` nebo
``claude``, nakonfiguruje je z ``--model`` / ``--effort`` a sjednotí jejich
výstup (viz :mod:`common.agentiscode`).

Použití::

    agentiscode --adapter opencode --model openai/gpt-5 --effort high "udelej X"
    agentiscode --adapter cloud --model claude-opus-4-8 "udelej X"
    echo "dlouhy prompt" | agentiscode --adapter claude --json

Bez ``--json`` jde odpověď agenta na stdout a aktivita (nástroje, reasoning,
souhrn) na stderr — klasický unixový tvar, kde stdout je výsledek. S ``--json``
jde na stdout postupně proud JSON řádků (JSON Lines) ve sjednoceném formátu.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from typing import Any, Dict, Optional, Sequence

from common.agentiscode import AgentConfig, AgentEvent, AgentWrapper, normalize_adapter


# ---------------------------------------------------------------------------
# Renderery
# ---------------------------------------------------------------------------


class JsonRenderer:
    """Vypíše každý sjednocený event jako jeden JSON řádek (JSON Lines) na stdout."""

    def handle(self, event: AgentEvent) -> None:
        sys.stdout.write(json.dumps(event.to_payload(), ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def finish(self) -> None:
        return None


class TextRenderer:
    """Lidsky čitelný výstup: odpověď agenta na stdout, aktivita na stderr."""

    def __init__(self) -> None:
        self._tool_names: Dict[str, str] = {}
        self._wrote_text = False

    def handle(self, event: AgentEvent) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            handler(event.data)

    def finish(self) -> None:
        if self._wrote_text:
            sys.stdout.write("\n")
            sys.stdout.flush()

    # -- jednotlivé typy ---------------------------------------------------

    def _on_session(self, data: Dict[str, Any]) -> None:
        parts = [str(data.get("adapter") or "agent")]
        if data.get("model"):
            parts.append(f"model={data['model']}")
        if data.get("session_id"):
            parts.append(f"session={data['session_id']}")
        self._stderr(f"⏺ {'  '.join(parts)}", dim=True)

    def _on_text(self, data: Dict[str, Any]) -> None:
        text = data.get("text") or ""
        if not text:
            return
        sys.stdout.write(text)
        sys.stdout.flush()
        self._wrote_text = True

    def _on_reasoning(self, data: Dict[str, Any]) -> None:
        text = (data.get("text") or "").strip()
        if text:
            self._stderr(f"  💭 {text}", dim=True)

    def _on_tool(self, data: Dict[str, Any]) -> None:
        call_id = data.get("id")
        status = data.get("status")
        if status == "running":
            name = data.get("name") or "tool"
            if isinstance(call_id, str):
                self._tool_names[call_id] = name
            title = data.get("title") or name
            label = f"{name}({title})" if title and title != name else name
            self._stderr(f"  ⚙ {label}")
        elif status == "error":
            name = self._tool_names.get(call_id, "tool") if isinstance(call_id, str) else "tool"
            err = (data.get("error") or data.get("output") or "").strip()
            self._stderr(f"  ✗ {name}: {err.splitlines()[0] if err else 'chyba'}")

    def _on_result(self, data: Dict[str, Any]) -> None:
        usage = data.get("usage") or {}
        bits = []
        if usage:
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            if inp is not None or out is not None:
                bits.append(f"tokens in={inp or 0} out={out or 0}")
        cost = data.get("cost_usd")
        if isinstance(cost, (int, float)):
            bits.append(f"cost=${cost:.4f}")
        suffix = ("  " + "  ".join(bits)) if bits else ""
        state = "error" if data.get("is_error") else "done"
        self._stderr(f"⏺ {state}{suffix}", dim=True)

    def _on_error(self, data: Dict[str, Any]) -> None:
        self._stderr(f"✗ {data.get('message') or 'chyba'}")

    def _on_stderr(self, data: Dict[str, Any]) -> None:
        line = (data.get("line") or "").rstrip()
        if line:
            self._stderr(f"  {line}", dim=True)

    @staticmethod
    def _stderr(text: str, *, dim: bool = False) -> None:
        if dim and sys.stderr.isatty():
            text = f"\033[2m{text}\033[0m"
        sys.stderr.write(text + "\n")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentiscode",
        description="Sjednocený commandline wrapper nad OpenCode a Claude Code.",
    )
    parser.add_argument(
        "--adapter",
        "-a",
        required=True,
        metavar="NAME",
        help="Agent runtime: opencode | claude (aliasy: cloud, claudecode, oc).",
    )
    parser.add_argument("--model", "-m", help="Model předaný agentovi.")
    parser.add_argument("--effort", "-e", help="Reasoning effort (Claude: --effort, OpenCode: --variant).")
    parser.add_argument("--agent", help="Pojmenovaný agent / mode CLI.")
    parser.add_argument("--cwd", help="Pracovní adresář (default: aktuální).")
    parser.add_argument("--resume", metavar="SESSION_ID", help="Naváže na existující session.")
    parser.add_argument("--timeout", type=float, default=0.0, help="Časový limit běhu v sekundách (0 = bez limitu).")
    parser.add_argument("--json", action="store_true", help="Streamuj sjednocené eventy jako JSON Lines na stdout.")
    parser.add_argument(
        "prompt",
        nargs="*",
        help="Zadání pro agenta. Když chybí, načte se ze stdin.",
    )
    return parser


def _read_prompt(parts: Sequence[str]) -> str:
    if parts:
        return " ".join(parts).strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


async def _run(config: AgentConfig, prompt: str, json_mode: bool) -> int:
    wrapper = AgentWrapper(config)
    renderer: Any = JsonRenderer() if json_mode else TextRenderer()

    proc_holder: Dict[str, Any] = {}

    def _on_proc(proc: asyncio.subprocess.Process) -> None:
        proc_holder["proc"] = proc

    exit_code = 0
    try:
        async for event in wrapper.stream(prompt, on_proc_started=_on_proc):
            if event.type == "error":
                exit_code = 1
            elif event.type == "result" and event.data.get("is_error"):
                exit_code = 1
            renderer.handle(event)
    except asyncio.CancelledError:
        _kill(proc_holder.get("proc"))
        raise
    finally:
        renderer.finish()
    return exit_code


def _kill(proc: Optional[asyncio.subprocess.Process]) -> None:
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(Exception):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    with contextlib.suppress(Exception):
        proc.kill()


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)

    try:
        normalize_adapter(args.adapter)
    except ValueError as exc:
        _parser().error(str(exc))

    prompt = _read_prompt(args.prompt)
    if not prompt:
        _parser().error("Chybí prompt (zadej ho jako argument nebo na stdin).")

    config = AgentConfig(
        adapter=args.adapter,
        model=args.model,
        effort=args.effort,
        agent=args.agent,
        cwd=args.cwd or os.getcwd(),
        resume_session_id=args.resume,
        timeout_sec=args.timeout,
    )

    try:
        return asyncio.run(_run(config, prompt, args.json))
    except KeyboardInterrupt:
        sys.stderr.write("\nPřerušeno.\n")
        return 130


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()


__all__ = ["run", "main", "JsonRenderer", "TextRenderer"]
