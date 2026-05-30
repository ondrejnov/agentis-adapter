"""
Mapper z `opencode run --format json` eventů do `session.store_activity_log`
tvaru ``[{"info": Message, "parts": [Part, ...]}, ...]``.

OpenCode streamuje pouze *part* eventy (text / reasoning / tool / step-start /
step-finish) — message info (role, tokeny, cost) musíme dopočítat. Mapper proto
udržuje jednu user zprávu (počáteční prompt) a pro každé ``messageID`` z part
eventů skládá assistant zprávu, do které postupně přidává/aktualizuje parts.

Tvar parts ponecháváme tak, jak je posílá OpenCode — odpovídá nativnímu
OpenCode transcriptu, který Agentis ``RunLogViewer`` umí vykreslit.
"""

from __future__ import annotations

import copy
import secrets
import time
from typing import Any, Dict, List, Optional
from uuid import UUID

from opencode.runner import OpenCodeEvent


def _now() -> float:
    return time.time()


def _uuid7() -> str:
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(UUID(int=value))


class OpenCodeActivityMapper:
    def __init__(
        self,
        prompt: str,
        *,
        session_id_hint: Optional[str] = None,
        mode: str = "build",
        agent: str = "build",
        provider_id: str = "opencode",
        cwd: Optional[str] = None,
    ) -> None:
        self.prompt = prompt
        self.mode = mode
        self.agent = agent
        self.provider_id = provider_id
        self.cwd = cwd
        self.model_id: Optional[str] = None
        self.session_id: str = session_id_hint or ""
        self._messages: List[Dict[str, Any]] = []
        # messageID -> index do self._messages (jen assistant zprávy)
        self._msg_idx: Dict[str, int] = {}
        # OpenCode messageID -> veřejné UUIDv7 message id v Agentis activity logu
        self._message_ids: Dict[str, str] = {}
        # messageID -> {partID -> index do parts}
        self._part_idx: Dict[str, Dict[str, int]] = {}
        # messageID -> {partID -> veřejné UUIDv7 part id v Agentis activity logu}
        self._part_ids: Dict[str, Dict[str, str]] = {}
        self._init_user_message()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._messages)

    def consume(self, event: OpenCodeEvent) -> bool:
        if event.type == "session_start":
            return self._on_session_start(event.data.get("session_id"))
        if event.type == "part":
            part = event.data.get("part")
            if isinstance(part, dict):
                return self._on_part(part)
            return False
        return False

    # ------------------------------------------------------------------
    # Inicializace
    # ------------------------------------------------------------------

    def _init_user_message(self) -> None:
        now = _now()
        message_id = _uuid7()
        info = {
            "id": message_id,
            "sessionID": self.session_id,
            "role": "user",
            "time": {"created": now},
        }
        parts: List[Dict[str, Any]] = []
        if self.prompt:
            parts.append(
                {
                    "id": _uuid7(),
                    "sessionID": self.session_id,
                    "messageID": message_id,
                    "type": "text",
                    "text": self.prompt,
                    "time": {"start": now, "end": now},
                }
            )
        self._messages.append({"info": info, "parts": parts})

    def _ensure_assistant(self, message_id: str) -> int:
        idx = self._msg_idx.get(message_id)
        if idx is not None:
            return idx
        activity_message_id = _uuid7()
        info: Dict[str, Any] = {
            "id": activity_message_id,
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
        idx = len(self._messages) - 1
        self._msg_idx[message_id] = idx
        self._message_ids[message_id] = activity_message_id
        self._part_idx[message_id] = {}
        self._part_ids[message_id] = {}
        return idx

    # ------------------------------------------------------------------
    # Handlery
    # ------------------------------------------------------------------

    def _on_session_start(self, session_id: Optional[str]) -> bool:
        if not session_id or session_id == self.session_id:
            return False
        self.session_id = session_id
        for entry in self._messages:
            entry["info"]["sessionID"] = session_id
            for part in entry["parts"]:
                part["sessionID"] = session_id
        return True

    def _on_part(self, part: Dict[str, Any]) -> bool:
        message_id = part.get("messageID")
        if not isinstance(message_id, str) or not message_id:
            return False
        part_id = part.get("id")
        if not isinstance(part_id, str) or not part_id:
            return False

        msg_index = self._ensure_assistant(message_id)
        stored = dict(part)
        stored.setdefault("sessionID", self.session_id)
        stored["messageID"] = self._message_ids[message_id]

        parts = self._messages[msg_index]["parts"]
        existing = self._part_idx[message_id].get(part_id)
        if existing is None:
            stored["id"] = _uuid7()
            self._part_ids[message_id][part_id] = stored["id"]
            parts.append(stored)
            self._part_idx[message_id][part_id] = len(parts) - 1
        else:
            stored["id"] = self._part_ids[message_id][part_id]
            parts[existing] = stored

        if part.get("type") == "step-finish":
            self._apply_step_finish(msg_index, part)
        return True

    def _apply_step_finish(self, msg_index: int, part: Dict[str, Any]) -> None:
        info = self._messages[msg_index]["info"]
        info.setdefault("time", {})["completed"] = _now()
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            info["tokens"] = {
                "input": int(tokens.get("input") or 0),
                "output": int(tokens.get("output") or 0),
                "reasoning": int(tokens.get("reasoning") or 0),
                "cache": {
                    "read": int(cache.get("read") or 0),
                    "write": int(cache.get("write") or 0),
                },
            }
        cost = part.get("cost")
        if isinstance(cost, (int, float)):
            info["cost"] = float(cost)
        reason = part.get("reason")
        if isinstance(reason, str) and reason:
            info["finish"] = reason
