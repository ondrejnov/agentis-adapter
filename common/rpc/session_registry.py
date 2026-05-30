"""In-memory mapping of opencode session_id -> AgentExecutionContextPayload.

The adapter is intentionally stateless across process restarts (see AGENTS.md),
but we still need to correlate incoming opencode events (which only carry a
session_id) back to the original execution context provided on `start`.
"""

from __future__ import annotations

import threading

from common.models import AgentExecutionContextPayload


class SessionContextRegistry:
    def __init__(self) -> None:
        self._store: dict[str, AgentExecutionContextPayload] = {}
        self._snapshot_keys: dict[str, str] = {}
        self._lock = threading.Lock()

    def register(self, session_id: str, context: AgentExecutionContextPayload) -> None:
        if not session_id:
            return
        with self._lock:
            self._store[session_id] = context

    def get(self, session_id: str) -> AgentExecutionContextPayload | None:
        with self._lock:
            return self._store.get(session_id)

    def set_snapshot_key(self, session_id: str, snapshot_key: str | None) -> None:
        if not session_id or not snapshot_key:
            return
        with self._lock:
            self._snapshot_keys[session_id] = snapshot_key

    def get_snapshot_key(self, session_id: str) -> str | None:
        with self._lock:
            return self._snapshot_keys.get(session_id)

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)
            self._snapshot_keys.pop(session_id, None)
