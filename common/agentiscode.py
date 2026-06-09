"""Sjednocená abstrakce nad CLI agenty OpenCode a Claude Code (``agentiscode``).

Oba agenti už mají vlastní tenký async wrapper na svoje CLI:

  - :class:`opencode.runner.OpenCodeRunner` — ``opencode run --format json``
  - :class:`claude.client.ClaudeCodeClient` — ``claude --print --output-format stream-json``

Každý z nich streamuje *svoje* normalizované eventy (``OpenCodeEvent`` /
``ClaudeEvent``), které se ale tvarem liší. Tenhle modul nad ně přidává jednu
společnou vrstvu: :class:`AgentWrapper` vybere podle ``--adapter`` ten správný
runner, nakonfiguruje ho z ``--model`` / ``--effort`` a jeho výstup přemapuje na
jednotný proud :class:`AgentEvent`.

Jednotný event slovník (``AgentEvent.type``):

  - ``session``   {adapter, session_id, model?, provider?, cwd?}
  - ``text``      {text, message_id?}    (assistant text — vždy *delta*, append-only)
  - ``reasoning`` {text, message_id?}    (reasoning/thinking — vždy delta)
  - ``tool``      {id, name?, status, input?, title?, output?, error?, message_id?}
  - ``step``      {usage?, cost_usd?, message_id?}  (jeden dokončený turn/message — per-turn usage)
  - ``result``    {session_id?, usage?, cost_usd?, is_error}
  - ``error``     {message}
  - ``stderr``    {line}

``message_id`` (u Claude adaptéru) označuje assistant zprávu, do které dílčí
event patří — díky němu lze ``text``/``reasoning``/``tool`` složit do jedné
zprávy a ``step`` (usage) započítat jen jednou per zpráva.

``step`` se vydává po každém dokončeném assistant turnu a nese usage *jen toho
turnu* (ne kumulativní). Díky tomu lze tokeny sčítat napříč turny — finální
``result`` u některých adaptérů (OpenCode) nese jen poslední turn / kontext.

``tool`` event se vydává při startu nástroje (``status="running"``) a znovu při
jeho dokončení (``status="completed"`` / ``"error"``); obě fáze sdílejí ``id``.

Tahle vrstva je úmyslně bez závislosti na Agentisu, Settings nebo Kubernetes —
je to čistě lokální wrapper. WebSocket transport (přihazování aktivity do
Agentisu) řeší samostatná aplikace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence

import asyncio

from claude.client import ClaudeCodeClient, ClaudeEvent, ClaudeRunConfig
from opencode.runner import OpenCodeEvent, OpenCodeRunConfig, OpenCodeRunner


# ---------------------------------------------------------------------------
# Adaptéry
# ---------------------------------------------------------------------------

OPENCODE = "opencode"
CLAUDE = "claude"

#: Uživatelsky zadané názvy adaptéru → kanonický klíč.
ADAPTER_ALIASES: Dict[str, str] = {
    "opencode": OPENCODE,
    "oc": OPENCODE,
    "claude": CLAUDE,
    "claudecode": CLAUDE,
    "claude-code": CLAUDE,
    "cloud": CLAUDE,
    "cc": CLAUDE,
}


def normalize_adapter(name: str) -> str:
    """Přeloží zadaný název adaptéru na kanonický (``opencode`` / ``claude``)."""
    key = (name or "").strip().lower()
    if key not in ADAPTER_ALIASES:
        choices = ", ".join(sorted(set(ADAPTER_ALIASES)))
        raise ValueError(f"Neznámý adaptér {name!r}. Dostupné: {choices}")
    return ADAPTER_ALIASES[key]


# ---------------------------------------------------------------------------
# Jednotný event a konfigurace
# ---------------------------------------------------------------------------


@dataclass
class AgentEvent:
    """Jedna sjednocená událost z běhu agenta (nezávislá na konkrétním CLI)."""

    type: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        return {"type": self.type, **self.data}


@dataclass
class AgentConfig:
    """Konfigurace jednoho běhu — společná pro oba adaptéry.

    ``effort`` je sjednocené reasoning úsilí: u Claude se mapuje na ``--effort``,
    u OpenCode na ``--variant`` (provider-specific reasoning effort).
    """

    adapter: str
    model: Optional[str] = None
    effort: Optional[str] = None
    agent: Optional[str] = None
    cwd: Optional[str] = None
    resume_session_id: Optional[str] = None
    timeout_sec: float = 0.0
    extra_args: Sequence[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tituly nástrojů (lidsky čitelná hlavička pro tool event)
# ---------------------------------------------------------------------------


def _with_msg_id(data: Dict[str, Any], msg_id: Optional[str]) -> Dict[str, Any]:
    """Přilepí ``message_id`` do payloadu, je-li k dispozici (jinak beze změny)."""
    if msg_id:
        data["message_id"] = msg_id
    return data


def _truncate(value: str, max_len: int = 80) -> str:
    value = (value or "").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def tool_title(name: Optional[str], raw_input: Any) -> str:
    """Vrátí krátký popisný titulek nástroje (cesta, příkaz, dotaz, …).

    Funguje pro Claude (``Read``, ``Bash``, …) i OpenCode (``read``, ``bash``,
    …) — název se porovnává case-insensitive.
    """
    fallback = name or "tool"
    if not isinstance(raw_input, dict):
        return fallback

    nl = (name or "").lower().strip()
    if nl in ("read", "edit", "multiedit", "write", "notebookedit"):
        path = raw_input.get("file_path") or raw_input.get("filePath") or raw_input.get("notebook_path")
        return _truncate(path) if isinstance(path, str) and path else fallback
    if nl == "bash":
        desc = raw_input.get("description")
        cmd = raw_input.get("command")
        if isinstance(desc, str) and desc.strip():
            return _truncate(desc)
        return _truncate(cmd) if isinstance(cmd, str) and cmd.strip() else fallback
    if nl in ("grep", "glob"):
        pattern = raw_input.get("pattern")
        return _truncate(pattern) if isinstance(pattern, str) and pattern else fallback
    if nl == "webfetch":
        url = raw_input.get("url")
        return _truncate(url, 120) if isinstance(url, str) and url else fallback
    if nl in ("websearch", "toolsearch"):
        query = raw_input.get("query")
        return _truncate(query, 120) if isinstance(query, str) and query else fallback
    if nl in ("task", "agent"):
        desc = raw_input.get("description")
        return _truncate(desc) if isinstance(desc, str) and desc.strip() else fallback
    return fallback


# ---------------------------------------------------------------------------
# Překladače nativních eventů → AgentEvent
# ---------------------------------------------------------------------------


class _ClaudeTranslator:
    """Claude Code stream-json eventy → jednotné :class:`AgentEvent`.

    Claude rozkládá jednu assistant zprávu (stejné ``message.id``) do více
    stream-json řádků a na každém opakuje identický ``usage``. ``client.py``
    proto protahuje ``message_id`` do dílčích eventů; tady ho přilepíme k
    ``text``/``reasoning``/``tool`` (ať konzument pozná, do které zprávy patří)
    a per-message ``usage`` vydáme jako ``step`` jen jednou (``_steps_seen``).
    """

    def __init__(self) -> None:
        self._steps_seen: set[str] = set()

    def __call__(self, event: ClaudeEvent) -> List[AgentEvent]:
        t = event.type
        d = event.data
        msg_id = d.get("message_id")
        if t == "session_start":
            return [
                AgentEvent(
                    "session",
                    {
                        "adapter": CLAUDE,
                        "session_id": d.get("session_id"),
                        "model": d.get("model"),
                        "provider": "anthropic",
                        "cwd": d.get("cwd"),
                    },
                )
            ]
        if t == "text":
            text = d.get("text") or ""
            return [AgentEvent("text", _with_msg_id({"text": text}, msg_id))] if text else []
        if t == "thinking":
            text = d.get("text") or ""
            return [AgentEvent("reasoning", _with_msg_id({"text": text}, msg_id))] if text else []
        if t == "tool_use":
            return [
                AgentEvent(
                    "tool",
                    _with_msg_id(
                        {
                            "id": d.get("id"),
                            "name": d.get("name"),
                            "status": "running",
                            "input": d.get("input"),
                            "title": tool_title(d.get("name"), d.get("input")),
                        },
                        msg_id,
                    ),
                )
            ]
        if t == "tool_result":
            return [
                AgentEvent(
                    "tool",
                    {
                        "id": d.get("tool_use_id"),
                        "status": "error" if d.get("is_error") else "completed",
                        "output": _stringify(d.get("content")),
                    },
                )
            ]
        if t == "result":
            return [
                AgentEvent(
                    "result",
                    {
                        "session_id": d.get("session_id"),
                        "usage": d.get("usage"),
                        "cost_usd": d.get("cost_usd"),
                        "is_error": bool(d.get("is_error")),
                    },
                )
            ]
        if t == "assistant_message":
            # Každá assistant zpráva nese vlastní `usage` toho turnu → vydáme
            # per-turn `step`, ať jde sčítat napříč turny (text/tool bloky už
            # máme z dílčích eventů, tady bereme jen usage). Tutéž zprávu (stejné
            # `message_id`) ale Claude posílá ve více chuncích s identickým usage,
            # takže `step` vydáme jen jednou per message_id.
            usage = (d.get("message") or {}).get("usage")
            if not isinstance(usage, dict) or not usage:
                return []
            if msg_id is not None:
                if msg_id in self._steps_seen:
                    return []
                self._steps_seen.add(msg_id)
            return [AgentEvent("step", _with_msg_id({"usage": dict(usage), "cost_usd": None}, msg_id))]
        if t == "error":
            return [AgentEvent("error", {"message": d.get("message")})]
        if t == "stderr":
            return [AgentEvent("stderr", {"line": d.get("line")})]
        # user_message / raw — vše podstatné už máme z dílčích eventů.
        return []


class _OpenCodeTranslator:
    """OpenCode ``run --format json`` eventy → jednotné :class:`AgentEvent`.

    OpenCode posílá text/reasoning party jako *snapshoty* (každý event nese celý
    dosavadní text té party), kdežto jednotný kontrakt chce *delty*. Sledujeme
    proto délku už vydaného textu per part-id a vydáváme jen přírůstek. Tool
    party přicházejí jako snapshoty stavu (running → completed/error); duplicitní
    ``running`` snapshoty zahazujeme přes ``_tool_status``.
    """

    def __init__(self) -> None:
        self._text_seen: Dict[str, int] = {}
        self._tool_status: Dict[str, str] = {}

    def __call__(self, event: OpenCodeEvent) -> List[AgentEvent]:
        t = event.type
        d = event.data
        if t == "session_start":
            return [
                AgentEvent(
                    "session",
                    {"adapter": OPENCODE, "session_id": d.get("session_id"), "provider": OPENCODE},
                )
            ]
        if t == "tool_before":
            return self._tool_event(
                call_id=d.get("callID"),
                name=d.get("tool"),
                status="running",
                input_=d.get("input"),
            )
        if t == "part":
            part = d.get("part")
            return self._on_part(part) if isinstance(part, dict) else []
        if t == "error":
            return [AgentEvent("error", {"message": d.get("message")})]
        if t == "stderr":
            return [AgentEvent("stderr", {"line": d.get("line")})]
        return []

    def _on_part(self, part: Dict[str, Any]) -> List[AgentEvent]:
        ptype = part.get("type")
        if ptype in ("text", "reasoning"):
            delta = self._text_delta(part)
            if not delta:
                return []
            return [AgentEvent("text" if ptype == "text" else "reasoning", {"text": delta})]
        if ptype == "tool":
            state = part.get("state") or {}
            extra: Dict[str, Any] = {}
            if state.get("output") is not None:
                extra["output"] = state.get("output")
            if state.get("error") is not None:
                extra["error"] = state.get("error")
            if state.get("title"):
                extra["title"] = state.get("title")
            return self._tool_event(
                call_id=part.get("callID"),
                name=part.get("tool"),
                status=state.get("status") or "running",
                input_=state.get("input"),
                extra=extra,
            )
        if ptype == "step-finish":
            # Per-turn usage — OpenCode posílá `step-finish` na každý turn, takže
            # tokeny lze sčítat (runner.last_usage drží jen poslední turn).
            return self._step_event(part)
        # step-start — souhrn už máme z dílčích eventů.
        return []

    @staticmethod
    def _step_event(part: Dict[str, Any]) -> List[AgentEvent]:
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            return []
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        usage = {
            "input_tokens": int(tokens.get("input") or 0),
            "output_tokens": int(tokens.get("output") or 0),
            "reasoning_tokens": int(tokens.get("reasoning") or 0),
            "cache_read_input_tokens": int(cache.get("read") or 0),
            "cache_creation_input_tokens": int(cache.get("write") or 0),
        }
        cost = part.get("cost")
        return [
            AgentEvent(
                "step",
                {"usage": usage, "cost_usd": float(cost) if isinstance(cost, (int, float)) else None},
            )
        ]

    def _text_delta(self, part: Dict[str, Any]) -> str:
        part_id = part.get("id")
        full = part.get("text") or ""
        if not isinstance(part_id, str) or not part_id:
            return full
        seen = self._text_seen.get(part_id, 0)
        self._text_seen[part_id] = len(full)
        return full[seen:]

    def _tool_event(
        self,
        *,
        call_id: Any,
        name: Any,
        status: str,
        input_: Any,
        extra: Optional[Dict[str, Any]] = None,
    ) -> List[AgentEvent]:
        if not isinstance(call_id, str) or not call_id:
            return []
        # Zahoď duplicitní snapshot stejného stavu (typicky opakovaný ``running``).
        if self._tool_status.get(call_id) == status and not (extra and (extra.get("output") or extra.get("error"))):
            return []
        self._tool_status[call_id] = status
        payload: Dict[str, Any] = {
            "id": call_id,
            "name": name if isinstance(name, str) else None,
            "status": status,
            "input": input_,
            "title": tool_title(name if isinstance(name, str) else None, input_),
        }
        if extra:
            payload.update(extra)
        return [AgentEvent("tool", payload)]


def _stringify(content: Any) -> str:
    """Sjednotí různé tvary tool výstupu Claude (str / list bloků / dict) na text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: List[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
            else:
                chunks.append(str(item))
        return "\n".join(chunks)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return str(content)


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------


class AgentWrapper:
    """Společné rozhraní nad OpenCode / Claude Code runnerem.

    ``stream(prompt)`` je async generátor jednotných :class:`AgentEvent`. Na konci
    proudu vždy zazní právě jeden ``result`` event — buď nativní (Claude), nebo
    dopočítaný z ``runner.last_usage`` / ``last_cost_usd`` (OpenCode).
    """

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.adapter = normalize_adapter(config.adapter)
        self._runner: Any = self._build_runner()
        self._translate = _OpenCodeTranslator() if self.adapter == OPENCODE else _ClaudeTranslator()

    # -- veřejné read-only summary z posledního běhu -----------------------

    @property
    def session_id(self) -> Optional[str]:
        return self._runner.session_id

    @property
    def last_usage(self) -> Optional[Dict[str, Any]]:
        return self._runner.last_usage

    @property
    def last_cost_usd(self) -> Optional[float]:
        return self._runner.last_cost_usd

    # -- runner ------------------------------------------------------------

    def _build_runner(self) -> Any:
        cfg = self.config
        env = {"IS_SANDBOX": "1", **dict(cfg.env)}
        if self.adapter == OPENCODE:
            return OpenCodeRunner(
                config=OpenCodeRunConfig(
                    cwd=cfg.cwd,
                    model=cfg.model,
                    agent=cfg.agent,
                    variant=cfg.effort,
                    resume_session_id=cfg.resume_session_id,
                    extra_args=cfg.extra_args,
                    env=env,
                    timeout_sec=cfg.timeout_sec,
                )
            )
        return ClaudeCodeClient(
            config=ClaudeRunConfig(
                cwd=cfg.cwd,
                model=cfg.model,
                agent=cfg.agent,
                effort=cfg.effort,
                resume_session_id=cfg.resume_session_id,
                extra_args=cfg.extra_args,
                env=env,
                timeout_sec=cfg.timeout_sec,
            )
        )

    # -- stream ------------------------------------------------------------

    async def stream(
        self,
        prompt: str,
        *,
        on_proc_started: Optional[Callable[[asyncio.subprocess.Process], None]] = None,
    ) -> AsyncIterator[AgentEvent]:
        emitted_result = False
        async for native in self._runner.stream(prompt, on_proc_started=on_proc_started):
            for unified in self._translate(native):
                if unified.type == "result":
                    emitted_result = True
                yield unified

        if not emitted_result:
            yield AgentEvent(
                "result",
                {
                    "session_id": self._runner.session_id,
                    "usage": self._runner.last_usage,
                    "cost_usd": self._runner.last_cost_usd,
                    "is_error": getattr(self._runner, "last_error", None) is not None,
                },
            )


__all__ = [
    "OPENCODE",
    "CLAUDE",
    "ADAPTER_ALIASES",
    "normalize_adapter",
    "AgentEvent",
    "AgentConfig",
    "AgentWrapper",
    "tool_title",
]
