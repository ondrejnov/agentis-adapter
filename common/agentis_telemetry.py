"""Volitelná telemetrie pro ``agentiscode``.

Když ``agentiscode`` dostane ``--task-id`` + ``--agentis-api``, založí v Agentisu
přes JSON-RPC nový **run** k danému tasku a průběžně do něj forwarduje aktivitu
agenta — úplně stejně, jako to dělá WebSocket transport (:class:`BaseSessionManager`),
jen pro sjednocený :class:`~common.agentiscode.AgentEvent` proud.

Posloupnost RPC volání zrcadlí websocket flow:

  1. ``task.start_run``        — založí run (bez spouštění adapteru) → ``run_id``
  2. ``run.store_session_id``  — naváže run na session agenta (nutné, než půjde
                                  ukládat aktivitu — Agentis hledá run podle session_id)
  3. ``session.store_activity_log`` — průběžně posílá transcript (OpenCode tvar)
  4. ``run.adapter_event``     — ``started`` na začátku, ``idle`` na konci běhu

Telemetrie je **best-effort**: žádná RPC chyba nesmí shodit samotný běh agenta,
proto se všechna volání obalují a případná chyba se jen ohlásí přes ``on_error``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from uuid import uuid4

from claude.activity_mapper import ClaudeActivityMapper
from claude.client import ClaudeEvent
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.agentiscode import AgentEvent


def _unified_to_native(event: AgentEvent) -> Optional[ClaudeEvent]:
    """Přemapuje sjednocený ``AgentEvent`` na nativní tvar pro ``ClaudeActivityMapper``.

    Mapper umí skládat transcript z Claude-stream-json eventů; sjednocený proud má
    jiné názvy typů, ale tytéž datové klíče, takže stačí jednoduché přejmenování.
    Vrací ``None`` pro eventy, které do transcriptu nepatří (``error`` / ``stderr``).
    """
    t = event.type
    d = event.data
    if t == "session":
        return ClaudeEvent(
            "session_start",
            {"session_id": d.get("session_id"), "model": d.get("model"), "cwd": d.get("cwd")},
        )
    if t == "text":
        return ClaudeEvent("text", {"text": d.get("text") or ""})
    if t == "reasoning":
        return ClaudeEvent("thinking", {"text": d.get("text") or ""})
    if t == "tool":
        status = d.get("status")
        if status == "running":
            return ClaudeEvent("tool_use", {"id": d.get("id"), "name": d.get("name"), "input": d.get("input")})
        if status in ("completed", "error"):
            is_error = status == "error"
            content = d.get("error") if is_error else d.get("output")
            return ClaudeEvent("tool_result", {"tool_use_id": d.get("id"), "content": content, "is_error": is_error})
        return None
    if t == "result":
        return ClaudeEvent(
            "result",
            {
                "session_id": d.get("session_id"),
                "usage": d.get("usage"),
                "cost_usd": d.get("cost_usd"),
                "is_error": bool(d.get("is_error")),
            },
        )
    return None


class AgentisTelemetry:
    """Forwarduje aktivitu jednoho ``agentiscode`` běhu do Agentis runu.

    Použití::

        with AgentisTelemetry(task_id=..., prompt=..., adapter="claude",
                              endpoint=..., token=...) as telemetry:
            telemetry.start()
            async for event in wrapper.stream(prompt):
                telemetry.handle(event)
            telemetry.finish()

    Pokud ``task.start_run`` nevrátí run id, telemetrie se tiše vypne
    (``handle`` / ``finish`` se stanou no-opem) a běh agenta pokračuje dál.
    """

    def __init__(
        self,
        *,
        task_id: str,
        prompt: str,
        adapter: str,
        mode: str = "build",
        cwd: Optional[str] = None,
        endpoint: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 10.0,
        client: Optional[AgentisJsonRpcClient] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        normalized_task_id = (task_id or "").strip()
        if not normalized_task_id:
            raise ValueError("task_id must not be empty")

        self.task_id = normalized_task_id
        self.adapter = adapter
        self.timeout = timeout
        self._on_error = on_error or (lambda message: None)

        self._client = client
        self._owns_client = client is None
        if client is None:
            if not endpoint:
                raise ValueError("endpoint must not be empty")
            self._client = AgentisJsonRpcClient(endpoint=endpoint, token=token, timeout=timeout)

        self._mapper = ClaudeActivityMapper(prompt=prompt, mode=mode, agent=adapter, cwd=cwd)
        self.run_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self._session_bound = False
        self._dirty = False
        self._is_error = False
        self._kind = f"{adapter}_run"

    def __enter__(self) -> "AgentisTelemetry":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    @property
    def active(self) -> bool:
        return self.run_id is not None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> Optional[str]:
        """Založí run k tasku (bez spuštění adapteru) a vrátí jeho ``run_id``."""
        result = self._call("task.start_run", {"id": self.task_id, "start_adapter": False})
        if isinstance(result, dict):
            item = result.get("item")
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                self.run_id = item["id"]
        if self.run_id is None:
            self._on_error("Agentis task.start_run nevrátil run id; telemetrie je vypnutá.")
            return None
        self._emit_adapter_event("started", message="agentiscode běh spuštěn.")
        return self.run_id

    def handle(self, event: AgentEvent) -> None:
        """Zpracuje jeden sjednocený event z běhu agenta."""
        if self.run_id is None:
            return

        if event.type == "error" or (event.type == "result" and event.data.get("is_error")):
            self._is_error = True

        if event.type == "session" and not self._session_bound:
            session_id = event.data.get("session_id")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id
                self._bind_session(session_id)

        native = _unified_to_native(event)
        if native is None:
            return
        if self._mapper.consume(native):
            self._dirty = True
            if self._session_bound:
                self._push_activity_log()

    def finish(self) -> None:
        """Dopošle zbylou aktivitu a uzavře run ``idle`` adapter eventem."""
        if self.run_id is None:
            return
        if self._session_bound and self._dirty:
            self._push_activity_log()
        status = "failed" if self._is_error else "success"
        message = "agentiscode běh selhal." if self._is_error else "agentiscode běh doběhl."
        self._emit_adapter_event(status, kind="idle", message=message)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _bind_session(self, session_id: str) -> None:
        # Agentis hledá run podle session_id, takže binding musí proběhnout dřív,
        # než dává smysl posílat `session.store_activity_log`.
        result = self._call("run.store_session_id", {"run_id": self.run_id, "session_id": session_id})
        self._session_bound = result is not None
        if self._session_bound and self._dirty:
            self._push_activity_log()

    def _push_activity_log(self) -> None:
        self._call("session.store_activity_log", {"session_id": self.session_id, "messages": self._mapper.snapshot()})
        self._dirty = False

    def _emit_adapter_event(
        self,
        status: str,
        *,
        kind: Optional[str] = None,
        message: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        event_kind = kind or self._kind
        self._call(
            "run.adapter_event",
            {
                "run_id": self.run_id,
                "kind": event_kind,
                "status": status,
                "event_id": f"{event_kind}:{self.run_id}:{status}",
                "message": message,
                "data": data or {},
            },
        )

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        if self._client is None:
            return None
        try:
            return self._client.call(method=method, params=params, request_id=f"agentiscode-{method}-{uuid4().hex}")
        except AgentisJsonRpcError as exc:
            self._on_error(f"Agentis {method} selhalo: {exc}")
        except Exception as exc:  # noqa: BLE001
            self._on_error(f"Agentis {method} nečekaná chyba: {exc!r}")
        return None


__all__ = ["AgentisTelemetry"]
