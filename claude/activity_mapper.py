"""
Mapper z Claude Code stream-json eventů do OpenCode-kompatibilního tvaru
``session.messages``.

OpenCode posílá do Agentisu (`session.store_activity_log`) pole zpráv ve tvaru:

    [
        {
            "info": UserMessage | AssistantMessage,
            "parts": [TextPart | ReasoningPart | ToolPart | StepFinishPart, ...],
        },
        ...
    ]

Tenhle modul ten samý tvar postupně skládá z eventů `ClaudeCodeClient.stream(...)`,
takže Claude Code lze ukládat do unified Agentis activity logu vedle OpenCodu.

Pravidla mapování:
  Claude event                         OpenCode part / pole
  ---------------------------------------------------------------
  system:init                          → metadata (session_id, model, cwd)
  assistant.content[text]              → TextPart
  assistant.content[thinking]          → ReasoningPart
  assistant.content[tool_use]          → ToolPart{state.status="running"}
  user.content[tool_result]            → ToolPart{state.status="completed"
                                          | "error" pokud is_error}
  result                               → uzavře assistant message
                                          (cost, tokens, finish reason)
                                          + přidá StepFinishPart

ToolPart se identifikuje přes ``callID == tool_use.id``; aktualizace tool_resultu
najde existující part a změní mu ``state``.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from claude.client import ClaudeEvent


# ---------------------------------------------------------------------------
# Pomocníci
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _prt_id() -> str:
    return f"prt_{uuid.uuid4().hex[:24]}"


def _ses_id_from(claude_session_id: Optional[str]) -> str:
    return claude_session_id or ""


# ---------------------------------------------------------------------------
# Normalizace tool inputu
# ---------------------------------------------------------------------------


def _truncate(value: str, max_len: int = 120) -> str:
    if not value:
        return ""
    value = value.strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def _short_path(value: str, cwd: Optional[str] = None) -> str:
    if not value:
        return ""
    # Pokud je cesta uvnitř worktree, ukaž ji relativně k němu.
    if cwd:
        cwd_norm = cwd.rstrip("/")
        if value == cwd_norm:
            return "."
        if value.startswith(cwd_norm + "/"):
            return value[len(cwd_norm) + 1 :]
    # Heuristika pro typické worktree pod "/var/www/agentiscraft/<task-uuid>/...".
    import re

    match = re.match(r"^/var/www/[^/]+/[0-9a-f-]{20,}/(.+)$", value)
    if match:
        return match.group(1)
    parts = value.split("/")
    if len(parts) > 5:
        return ".../" + "/".join(parts[-4:])
    return value


def _normalize_tool_input(name: Optional[str], raw_input: Any, cwd: Optional[str] = None) -> tuple[Dict[str, Any], str]:
    """Vrátí (vstupy ve tvaru srozumitelném pro frontend, popisný title).

    Frontend (Agentis ``RunLogViewer``) preferuje camelCase klíče (``filePath``)
    a používá ``state.title`` jako hlavičku karty nástroje. Claude Code posílá
    snake_case (``file_path``) a generický ``state.title=tool_name``. Tato
    funkce sjednotí oba světy: zkopíruje hodnoty pod camelCase aliasy
    a doplní popisný titulek (cesta k souboru, příkaz, dotaz, ...).
    """

    fallback_title = name or "tool"
    if not isinstance(raw_input, dict):
        return ({} if raw_input is None else {"value": raw_input}), fallback_title

    inp: Dict[str, Any] = dict(raw_input)
    nl = (name or "").lower().strip()
    title = fallback_title

    def _alias(*pairs: tuple[str, str]) -> None:
        for src, dst in pairs:
            if src in inp and dst not in inp and inp[src] is not None:
                inp[dst] = inp[src]

    if nl == "read":
        _alias(("file_path", "filePath"), ("notebook_path", "filePath"))
        path = inp.get("filePath") or inp.get("file_path")
        if isinstance(path, str) and path:
            title = _short_path(path, cwd)
    elif nl in ("edit", "multiedit", "write", "notebookedit"):
        _alias(("file_path", "filePath"), ("notebook_path", "filePath"))
        path = inp.get("filePath") or inp.get("file_path")
        if isinstance(path, str) and path:
            title = _short_path(path, cwd)
    elif nl == "bash":
        cmd = inp.get("command")
        desc = inp.get("description")
        if isinstance(desc, str) and desc.strip():
            title = _truncate(desc, 80)
        elif isinstance(cmd, str) and cmd.strip():
            title = _truncate(cmd, 80)
    elif nl == "glob":
        pattern = inp.get("pattern")
        path = inp.get("path")
        if isinstance(pattern, str) and pattern:
            title = _truncate(pattern, 80)
            if isinstance(path, str) and path:
                title = f"{title}  (in {_short_path(path, cwd)})"
    elif nl == "grep":
        pattern = inp.get("pattern")
        if isinstance(pattern, str) and pattern:
            title = _truncate(pattern, 80)
    elif nl == "todowrite":
        todos = inp.get("todos")
        if isinstance(todos, list):
            title = f"{len(todos)} todos"
    elif nl in ("task", "agent"):
        _alias(("subagent_type", "subagentType"))
        desc = inp.get("description")
        prompt = inp.get("prompt")
        subagent = inp.get("subagentType") or inp.get("subagent_type")
        if isinstance(desc, str) and desc.strip():
            title = _truncate(desc, 80)
        elif isinstance(subagent, str) and subagent.strip():
            title = _truncate(subagent, 80)
        elif isinstance(prompt, str) and prompt.strip():
            title = _truncate(prompt, 80)
    elif nl == "webfetch":
        url = inp.get("url")
        if isinstance(url, str) and url:
            title = _truncate(url, 120)
    elif nl in ("websearch", "toolsearch"):
        query = inp.get("query")
        if isinstance(query, str) and query:
            title = _truncate(query, 120)
    elif nl == "askuserquestion":
        questions = inp.get("questions")
        if isinstance(questions, list):
            title = f"Asked {len(questions)} questions"

    return inp, title


# ---------------------------------------------------------------------------
# Stav
# ---------------------------------------------------------------------------


@dataclass
class _AssistantState:
    msg_idx: int
    claude_msg_id: Optional[str] = None  # `msg_01...` od Claudu — pro dedup
    text_part_idx: Optional[int] = None
    reasoning_part_idx: Optional[int] = None
    tool_part_idx: Dict[str, int] = field(default_factory=dict)  # callID → part idx
    usage_recorded: bool = False  # usage už zapsané (per claude_msg_id) → nezdvojovat
    closed: bool = False


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


class ClaudeActivityMapper:
    """
    Postupně staví transcript v OpenCode tvaru ze streamu Claude Code eventů.

    Použití:

        mapper = ClaudeActivityMapper(prompt="...", session_id_hint=None,
                                       mode="build", agent="claude")
        async for event in client.stream(prompt):
            changed = mapper.consume(event)
            if changed:
                rpc.call("session.store_activity_log", {
                    "session_id": mapper.session_id,
                    "messages": mapper.snapshot(),
                })

    Pro zaslání jen na konci stačí volat `mapper.snapshot()` po dokončení streamu.
    """

    def __init__(
        self,
        prompt: str,
        *,
        session_id_hint: Optional[str] = None,
        mode: str = "build",
        agent: str = "claude",
        provider_id: str = "anthropic",
        cwd: Optional[str] = None,
    ) -> None:
        self.prompt = prompt
        self.mode = mode
        self.agent = agent
        self.provider_id = provider_id
        self.cwd = cwd
        self.model_id: Optional[str] = None
        self._claude_session_id: Optional[str] = session_id_hint
        self.session_id: str = _ses_id_from(session_id_hint)
        self._messages: List[Dict[str, Any]] = []
        self._user_msg_idx: Optional[int] = None
        self._assistant: Optional[_AssistantState] = None
        # True, jakmile zapíšeme per-turn tokeny (z assistant zpráv / step-finishů).
        # Pak finální `result` (kumulativní) už do transcriptu nepouštíme, ať se
        # tokeny nezdvojí.
        self._tokens_from_steps = False
        self._init_user_message()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(self) -> List[Dict[str, Any]]:
        """Vrátí hlubokou kopii aktuálního transcriptu (bezpečné pro odeslání)."""
        return copy.deepcopy(self._messages)

    def consume(self, event: ClaudeEvent) -> bool:
        """
        Zkonzumuje jeden event. Vrátí ``True`` pokud se transcript změnil
        (= má smysl poslat nový snapshot do `session.store_activity_log`).
        """
        handlers = {
            "session_start": self._on_session_start,
            "text": self._on_text,
            "thinking": self._on_thinking,
            "tool_use": self._on_tool_use,
            "tool_result": self._on_tool_result,
            "assistant_message": self._on_assistant_message,
            "step_finish": self._on_step_finish,
            "result": self._on_result,
            # text/thinking/tool_use/tool_result skládají obsah zprávy;
            # assistant_message slouží jen k zápisu per-turn tokenů (usage).
            # user_message a raw/stderr/error jsou jen logy.
        }
        handler = handlers.get(event.type)
        if not handler:
            return False
        return bool(handler(event.data))

    # ------------------------------------------------------------------
    # Inicializace
    # ------------------------------------------------------------------

    def _init_user_message(self) -> None:
        msg_id = _msg_id()
        prt_id = _prt_id()
        now = _now()
        info = {
            "id": msg_id,
            "sessionID": self.session_id,
            "role": "user",
            "time": {"created": now},
        }
        parts: List[Dict[str, Any]] = []
        if self.prompt:
            parts.append(
                {
                    "id": prt_id,
                    "sessionID": self.session_id,
                    "messageID": msg_id,
                    "type": "text",
                    "text": self.prompt,
                    "time": {"start": now, "end": now},
                }
            )
        self._messages.append({"info": info, "parts": parts})
        self._user_msg_idx = len(self._messages) - 1

    def _ensure_assistant(self, claude_msg_id: Optional[str] = None) -> _AssistantState:
        # Pokud je aktuální assistant zpráva uzavřená nebo má jiné claude id, založ novou.
        if self._assistant is not None and not self._assistant.closed:
            if claude_msg_id is None or self._assistant.claude_msg_id is None:
                self._assistant.claude_msg_id = self._assistant.claude_msg_id or claude_msg_id
                return self._assistant
            if self._assistant.claude_msg_id == claude_msg_id:
                return self._assistant
            # nové claude msg id → uzavři předchozí (dopíše mu step-finish)
            self._close_assistant(self._assistant)

        msg_id = _msg_id()
        info: Dict[str, Any] = {
            "id": msg_id,
            "sessionID": self.session_id,
            "role": "assistant",
            "time": {"created": _now()},
            "modelID": self.model_id or "",
            "providerID": self.provider_id,
            "mode": self.mode,
            "agent": self.agent,
            "path": {"cwd": self.cwd or "", "root": self.cwd or ""},
            "cost": 0,
            "tokens": {
                "input": 0,
                "output": 0,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
        }
        self._messages.append({"info": info, "parts": []})
        self._assistant = _AssistantState(
            msg_idx=len(self._messages) - 1,
            claude_msg_id=claude_msg_id,
        )
        return self._assistant

    def _assistant_message_id(self, state: _AssistantState) -> str:
        return self._messages[state.msg_idx]["info"]["id"]

    # ------------------------------------------------------------------
    # Handlery
    # ------------------------------------------------------------------

    def _on_session_start(self, data: Dict[str, Any]) -> bool:
        self._claude_session_id = data.get("session_id") or self._claude_session_id
        if data.get("model"):
            self.model_id = data["model"]
        if data.get("cwd"):
            self.cwd = data["cwd"]
        new_session_id = _ses_id_from(self._claude_session_id)
        if new_session_id != self.session_id:
            self.session_id = new_session_id
            for entry in self._messages:
                entry["info"]["sessionID"] = self.session_id
                for part in entry["parts"]:
                    part["sessionID"] = self.session_id
        return True

    def _on_text(self, data: Dict[str, Any]) -> bool:
        text = data.get("text") or ""
        if not text:
            return False
        state = self._ensure_assistant(data.get("message_id"))
        msg_id = self._assistant_message_id(state)
        parts = self._messages[state.msg_idx]["parts"]
        if state.text_part_idx is None:
            parts.append(
                {
                    "id": _prt_id(),
                    "sessionID": self.session_id,
                    "messageID": msg_id,
                    "type": "text",
                    "text": text,
                    "time": {"start": _now()},
                }
            )
            state.text_part_idx = len(parts) - 1
        else:
            part = parts[state.text_part_idx]
            part["text"] = (part.get("text") or "") + text
        return True

    def _on_thinking(self, data: Dict[str, Any]) -> bool:
        text = data.get("text") or ""
        if not text:
            return False
        state = self._ensure_assistant(data.get("message_id"))
        msg_id = self._assistant_message_id(state)
        parts = self._messages[state.msg_idx]["parts"]
        if state.reasoning_part_idx is None:
            parts.append(
                {
                    "id": _prt_id(),
                    "sessionID": self.session_id,
                    "messageID": msg_id,
                    "type": "reasoning",
                    "text": text,
                    "time": {"start": _now()},
                }
            )
            state.reasoning_part_idx = len(parts) - 1
        else:
            part = parts[state.reasoning_part_idx]
            part["text"] = (part.get("text") or "") + text
        return True

    def _on_tool_use(self, data: Dict[str, Any]) -> bool:
        call_id = data.get("id")
        if not call_id:
            return False
        state = self._ensure_assistant(data.get("message_id"))
        if call_id in state.tool_part_idx:
            return False
        msg_id = self._assistant_message_id(state)
        parts = self._messages[state.msg_idx]["parts"]
        tool_name = data.get("name") or ""
        normalized_input, title = _normalize_tool_input(tool_name, data.get("input"), self.cwd)
        now = _now()
        parts.append(
            {
                "id": _prt_id(),
                "sessionID": self.session_id,
                "messageID": msg_id,
                "type": "tool",
                "callID": call_id,
                "tool": tool_name,
                "state": {
                    "status": "running",
                    "input": normalized_input,
                    "title": title,
                    "metadata": {},
                    "time": {"start": now},
                },
            }
        )
        state.tool_part_idx[call_id] = len(parts) - 1
        # Po tool_use se případný další text patří do nové text-part
        # (jako step v OpenCode) — nuluj index, ať se nesleduje předchozí text.
        state.text_part_idx = None
        state.reasoning_part_idx = None
        return True

    def _on_tool_result(self, data: Dict[str, Any]) -> bool:
        call_id = data.get("tool_use_id")
        if not call_id:
            return False
        # Najdi ToolPart napříč zprávami (může být v dřívější assistant zprávě)
        part = self._find_tool_part(call_id)
        if part is None:
            return False
        is_error = bool(data.get("is_error"))
        output = self._stringify_tool_output(data.get("content"))
        old_state = part.get("state") or {}
        time_obj = dict(old_state.get("time") or {})
        time_obj["end"] = _now()
        if is_error:
            part["state"] = {
                "status": "error",
                "input": old_state.get("input") or {},
                "error": output,
                "metadata": old_state.get("metadata") or {},
                "time": time_obj,
            }
        else:
            part["state"] = {
                "status": "completed",
                "input": old_state.get("input") or {},
                "output": output,
                "title": old_state.get("title") or part.get("tool") or "",
                "metadata": old_state.get("metadata") or {},
                "time": time_obj,
            }
        return True

    @staticmethod
    def _tokens_from_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        usage = usage or {}
        return {
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
            "reasoning": int(usage.get("reasoning_tokens") or 0),
            "cache": {
                "read": int(usage.get("cache_read_input_tokens") or 0),
                "write": int(usage.get("cache_creation_input_tokens") or usage.get("cache_write_tokens") or 0),
            },
        }

    def _record_usage(self, state: _AssistantState, usage: Optional[Dict[str, Any]], cost: Any, finish: str) -> None:
        """Zapíše do assistant zprávy její *vlastní* tokeny a cost. Idempotentní —
        opakované volání jen přepíše stejné hodnoty (Claude posílá identický usage
        na každém chunku téže zprávy)."""
        info = self._messages[state.msg_idx]["info"]
        info["time"]["completed"] = _now()
        info["tokens"] = self._tokens_from_usage(usage)
        if isinstance(cost, (int, float)):
            info["cost"] = float(cost)
        info["finish"] = finish
        state.usage_recorded = True

    def _close_assistant(self, state: _AssistantState) -> bool:
        """Uzavře assistant zprávu a přidá jí StepFinishPart (jeden na zprávu).
        Voláno při přechodu na nové claude msg id nebo na finálním `result`."""
        if state.closed:
            return False
        info = self._messages[state.msg_idx]["info"]
        # StepFinishPart — užitečné pro UI v Agentisu
        self._messages[state.msg_idx]["parts"].append(
            {
                "id": _prt_id(),
                "sessionID": self.session_id,
                "messageID": info["id"],
                "type": "step-finish",
                "reason": info.get("finish") or "stop",
                "cost": info.get("cost") or 0,
                "tokens": info.get("tokens") or self._tokens_from_usage(None),
            }
        )
        state.closed = True
        return True

    def _finalize_assistant(self, usage: Optional[Dict[str, Any]], cost: Any, finish: str) -> bool:
        """Zapíše tokeny a rovnou zprávu uzavře (jeden turn = jedna zpráva).
        Používá se tam, kde nemáme claude msg id pro seskupení (legacy assistant
        zprávy bez id, `step_finish`, fallback z `result`)."""
        state = self._ensure_assistant()
        self._record_usage(state, usage, cost, finish)
        self._close_assistant(state)
        return True

    def _on_assistant_message(self, data: Dict[str, Any]) -> bool:
        # Claude posílá `usage` na každé assistant zprávě a tutéž zprávu (stejné
        # `message.id`) rozkládá do více chunků s IDENTICKÝM usage. Bez id je každá
        # zpráva hranicí turnu; s id ji seskupíme a usage zapíšeme jen jednou, ať
        # se tokeny nepočítají dvakrát.
        message = data.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict) or not usage:
            return False
        self._tokens_from_steps = True
        claude_msg_id = data.get("message_id") or (message.get("id") if isinstance(message, dict) else None)
        if not claude_msg_id:
            # Legacy: bez id bereme každou assistant zprávu jako samostatný turn.
            return self._finalize_assistant(usage, None, "stop")
        state = self._ensure_assistant(claude_msg_id)
        if state.usage_recorded:
            # Tutéž zprávu už máme spočítanou; další chunky neopakuj.
            return False
        self._record_usage(state, usage, None, "stop")
        return True

    def _on_step_finish(self, data: Dict[str, Any]) -> bool:
        self._tokens_from_steps = True
        return self._finalize_assistant(data.get("usage"), data.get("cost_usd"), "stop")

    def _on_result(self, data: Dict[str, Any]) -> bool:
        if self._tokens_from_steps:
            # Per-turn tokeny už máme; finální `result` je kumulativní a jen by je
            # zopakoval → tokeny nepřidáváme. Jen dovřeme poslední otevřenou zprávu,
            # ať dostane svůj step-finish (run-end řeší adapter event).
            if self._assistant is not None and not self._assistant.closed:
                return self._close_assistant(self._assistant)
            return False
        finish = "stop" if not data.get("is_error") else (data.get("subtype") or "error")
        return self._finalize_assistant(data.get("usage"), data.get("cost_usd"), finish)

    # ------------------------------------------------------------------
    # Util
    # ------------------------------------------------------------------

    def _find_tool_part(self, call_id: str) -> Optional[Dict[str, Any]]:
        # Nejprve v aktuální assistant zprávě, pak hledej zpětně.
        for entry in reversed(self._messages):
            for part in entry["parts"]:
                if part.get("type") == "tool" and part.get("callID") == call_id:
                    return part
        return None

    @staticmethod
    def _stringify_tool_output(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and isinstance(item.get("text"), str):
                        chunks.append(item["text"])
                    elif "text" in item and isinstance(item["text"], str):
                        chunks.append(item["text"])
                    else:
                        chunks.append(str(item))
                else:
                    chunks.append(str(item))
            return "\n".join(chunks)
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                return content["text"]
        return str(content)
