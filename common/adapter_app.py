from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException

from common.config import Settings
from common.rpc.dispatcher import JsonRpcRoute
from common.rpc.session_registry import SessionContextRegistry
from common.status import get_status_registry


__all__ = ["JsonRpcRoute", "create_adapter_app"]


def create_adapter_app(
    *,
    title: str,
    settings: Settings,
    configure_services: Callable[[FastAPI, Settings, SessionContextRegistry], None],
    version: str = "0.1.0",
) -> FastAPI:
    """Build the adapter's service container.

    External Agentis JSON-RPC (``start``, ``add_message`` …) is delivered over the
    passive WebSocket transport, not over HTTP. The agent runtime no longer calls
    back into the adapter (its activity is streamed directly from the ``opencode``/
    ``claude`` CLI). The HTTP app holds the configured services on ``app.state``
    and serves only read-only observability endpoints (``/health``, ``/status``,
    logy) pro lokální TUI ``agentis-top``.
    """
    app = FastAPI(title=title, version=version)
    session_registry = SessionContextRegistry()
    app.state.session_registry = session_registry
    configure_services(app, settings, session_registry)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return get_status_registry().snapshot()

    @app.get("/log")
    async def adapter_log(after: int = 0, limit: int = 500) -> dict[str, Any]:
        return {"entries": get_status_registry().log_entries(after=after, limit=limit)}

    @app.get("/runs/{run_id}/log")
    async def run_log(run_id: str, after: int = 0, limit: int = 500) -> dict[str, Any]:
        entries = get_status_registry().run_log_entries(run_id, after=after, limit=limit)
        if entries is None:
            raise HTTPException(status_code=404, detail=f"Unknown run {run_id}")
        return {"entries": entries}

    return app
