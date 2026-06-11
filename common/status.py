"""In-memory stav adapteru pro lokální observabilitu (TUI `agentis-top`, `/status`).

`StatusRegistry` sbírá stav WebSocket spojení na Agentis, záznamy o runech
(agentí session i workflow), aktivitu per run a globální log adapteru.
Vše je čistě in-memory ring buffer — adapter zůstává stateless přes restarty.

Registry je modulový singleton (`get_status_registry`), protože do něj zapisují
nezávislé vrstvy (log_json, WebSocket transport, session manager, workflow
manager) bez společného service kontejneru.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

#: Kolik dokončených runů držet pro historii v `/status`.
_FINISHED_RUNS_LIMIT = 50
#: Kapacita globálního log ring bufferu.
_LOG_BUFFER_LIMIT = 2000
#: Kapacita activity ring bufferu jednoho runu.
_RUN_ACTIVITY_LIMIT = 200
#: Maximální délka jednoho activity řádku.
_ACTIVITY_TEXT_LIMIT = 200

_RUN_FINISHED_STATUSES = {"success", "failed", "aborted"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, limit: int = _ACTIVITY_TEXT_LIMIT) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _scrub_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if "token" not in key.lower()}


@dataclass
class RunRecord:
    run_id: str
    task_id: str = ""
    task_number: Optional[int] = None
    title: str = ""
    #: "agent" (CLI session) | "workflow" | "unknown" (lazy záznam mimo RPC vrstvu).
    kind: str = "unknown"
    #: RPC metoda, která run založila ("start" | "add_message").
    method: str = ""
    #: Název pojmenovaného workflow (merge, close, ...), pokud jde o followup akci.
    workflow: Optional[str] = None
    status: str = "running"
    received_at: str = field(default_factory=_iso_now)
    received_monotonic: float = field(default_factory=time.monotonic)
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    session_id: Optional[str] = None
    worktree: Optional[str] = None
    last_activity: Optional[str] = None
    last_activity_at: Optional[str] = None
    activity: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=_RUN_ACTIVITY_LIMIT))
    _activity_seq: int = 0

    def dump(self) -> dict[str, Any]:
        running = self.status == "running"
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "task_number": self.task_number,
            "title": self.title,
            "kind": self.kind,
            "method": self.method,
            "workflow": self.workflow,
            "status": self.status,
            "received_at": self.received_at,
            "finished_at": self.finished_at,
            "duration_seconds": (
                round(time.monotonic() - self.received_monotonic, 3) if running else self.duration_seconds
            ),
            "session_id": self.session_id,
            "worktree": self.worktree,
            "last_activity": self.last_activity,
            "last_activity_at": self.last_activity_at,
        }


class StatusRegistry:
    """Thread-safe sběrné místo pro stav adapteru; čtou ho status endpointy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = _iso_now()
        self._started_monotonic = time.monotonic()
        self._meta: dict[str, Any] = {}
        self._ws: dict[str, Any] = {"state": "disconnected", "since": self.started_at}
        self._ws_reconnects = 0
        self._runs: dict[str, RunRecord] = {}
        self._finished_order: deque[str] = deque()
        self._counters: dict[str, int] = {
            "runs_received": 0,
            "runs_succeeded": 0,
            "runs_failed": 0,
            "runs_aborted": 0,
            "messages_received": 0,
            "aborts_received": 0,
        }
        self._durations_total = 0.0
        self._durations_count = 0
        self._log: deque[dict[str, Any]] = deque(maxlen=_LOG_BUFFER_LIMIT)
        self._log_seq = 0

    # ------------------------------------------------------------------
    # Meta + WebSocket stav
    # ------------------------------------------------------------------

    def set_meta(self, **values: Any) -> None:
        with self._lock:
            self._meta.update({key: value for key, value in values.items() if value is not None})

    def ws_connecting(self, endpoint: str | None, attempt: int) -> None:
        with self._lock:
            self._ws = {"state": "connecting", "endpoint": endpoint, "since": _iso_now(), "attempt": attempt}

    def ws_connected(self, endpoint: str | None) -> None:
        with self._lock:
            self._ws = {"state": "connected", "endpoint": endpoint, "since": _iso_now()}

    def ws_disconnected(self, endpoint: str | None, error: str | None = None) -> None:
        with self._lock:
            if self._ws.get("state") == "connected":
                self._ws_reconnects += 1
            self._ws = {"state": "disconnected", "endpoint": endpoint, "since": _iso_now(), "last_error": error}

    # ------------------------------------------------------------------
    # Runy
    # ------------------------------------------------------------------

    def _record(self, run_id: str) -> RunRecord:
        record = self._runs.get(run_id)
        if record is None:
            record = RunRecord(run_id=run_id)
            self._runs[run_id] = record
        return record

    def run_received(self, context: Any, *, kind: str, method: str) -> None:
        """Zaeviduje příchozí RPC run; volá se na začátku `start`/`add_message`."""
        adapter = getattr(context, "adapter", None)
        with self._lock:
            record = self._record(context.run_id)
            record.task_id = context.task_id
            record.task_number = getattr(context, "task_number", None)
            record.title = context.title or ""
            record.kind = kind
            record.method = method
            record.workflow = getattr(adapter, "workflow", None) if adapter else None
            record.session_id = getattr(context, "session_id", None) or record.session_id
            self._counters["runs_received"] += 1
            if method == "add_message":
                self._counters["messages_received"] += 1

    def run_update(self, run_id: str, **values: Any) -> None:
        if not run_id:
            return
        with self._lock:
            record = self._record(run_id)
            for key, value in values.items():
                if value is not None and hasattr(record, key):
                    setattr(record, key, value)

    def run_activity(self, run_id: str, text: str) -> None:
        if not run_id or not text:
            return
        entry_text = _truncate(text)
        now = _iso_now()
        with self._lock:
            record = self._record(run_id)
            record._activity_seq += 1
            record.activity.append({"seq": record._activity_seq, "timestamp": now, "text": entry_text})
            record.last_activity = entry_text
            record.last_activity_at = now

    def run_finished(self, run_id: str, status: str) -> None:
        if not run_id:
            return
        normalized = status if status in _RUN_FINISHED_STATUSES else "failed"
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.status != "running":
                return
            record.status = normalized
            record.finished_at = _iso_now()
            record.duration_seconds = round(time.monotonic() - record.received_monotonic, 3)
            self._counters[f"runs_{'succeeded' if normalized == 'success' else normalized}"] += 1
            self._durations_total += record.duration_seconds
            self._durations_count += 1
            self._finished_order.append(run_id)
            while len(self._finished_order) > _FINISHED_RUNS_LIMIT:
                oldest = self._finished_order.popleft()
                self._runs.pop(oldest, None)

    def abort_received(self) -> None:
        with self._lock:
            self._counters["aborts_received"] += 1

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------

    def log(self, level: str, message: str, fields: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._log_seq += 1
            self._log.append(
                {
                    "seq": self._log_seq,
                    "timestamp": _iso_now(),
                    "level": level,
                    "message": message,
                    "fields": _scrub_fields(fields or {}),
                }
            )

    def log_entries(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            return [entry for entry in self._log if entry["seq"] > after][:limit]

    def run_log_entries(self, run_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]] | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            return [entry for entry in record.activity if entry["seq"] > after][:limit]

    # ------------------------------------------------------------------
    # Snapshot pro /status
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = [record.dump() for record in self._runs.values() if record.status == "running"]
            finished = [
                self._runs[run_id].dump() for run_id in reversed(self._finished_order) if run_id in self._runs
            ]
            running.sort(key=lambda item: item["received_at"])
            stats = dict(self._counters)
            stats["runs_running"] = len(running)
            stats["ws_reconnects"] = self._ws_reconnects
            stats["avg_run_duration_seconds"] = (
                round(self._durations_total / self._durations_count, 3) if self._durations_count else None
            )
            return {
                **self._meta,
                "started_at": self.started_at,
                "uptime_seconds": round(time.monotonic() - self._started_monotonic, 3),
                "websocket": dict(self._ws),
                "runs": {"running": running, "finished": finished},
                "stats": stats,
                "log_seq": self._log_seq,
            }


def activity_from_event(event_type: str, data: dict[str, Any] | None) -> str | None:
    """Sestaví kompaktní popis aktivity z normalizovaného CLI eventu.

    Vrací ``None`` pro eventy, které v přehledu aktivity jen šumí
    (raw řádky, stderr, složené assistant zprávy, tool výsledky).
    """
    data = data or {}
    if event_type == "session_start":
        model = data.get("model")
        return f"session start ({model})" if model else "session start"
    if event_type == "tool_use":
        name = data.get("name") or "tool"
        tool_input = data.get("input") or {}
        detail = ""
        if isinstance(tool_input, dict):
            detail = str(
                tool_input.get("file_path") or tool_input.get("command") or tool_input.get("pattern") or ""
            )
        return _truncate(f"{name} {detail}".strip(), 120)
    if event_type == "text":
        text = (data.get("text") or "").strip()
        return _truncate(text, 120) if text else None
    if event_type == "thinking":
        return "přemýšlí…"
    if event_type == "error":
        return _truncate(f"chyba: {data.get('message') or 'unknown'}", 160)
    return None


_registry = StatusRegistry()


def get_status_registry() -> StatusRegistry:
    return _registry


def reset_status_registry() -> StatusRegistry:
    """Vymění singleton za čistou instanci (izolace testů)."""
    global _registry
    _registry = StatusRegistry()
    return _registry


__all__ = [
    "RunRecord",
    "StatusRegistry",
    "activity_from_event",
    "get_status_registry",
    "reset_status_registry",
]
