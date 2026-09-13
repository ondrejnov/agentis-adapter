from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from common.adapter_base import log_json
from common.config import Settings
from common.rpc.dispatcher import JsonRpcRoute
from common.status import get_status_registry


__all__ = ["JsonRpcRoute", "create_adapter_app"]


def create_adapter_app(
    *,
    title: str,
    settings: Settings,
    configure_services: Callable[[FastAPI, Settings], None],
    version: str = "0.1.0",
) -> FastAPI:
    """Build the adapter's service container.

    External Agentis JSON-RPC (``start``, ``add_message`` …) is delivered over the
    adapter-initiated WebSocket transport, not over HTTP. The agent runtime no longer calls
    back into the adapter (its activity is streamed directly from the ``opencode``/
    ``claude`` CLI). The HTTP app holds the configured services on ``app.state``
    and serves only read-only observability endpoints (``/health``, ``/status``,
    logs).
    """
    app = FastAPI(title=title, version=version)
    configure_services(app, settings)

    @app.middleware("http")
    async def log_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        finally:
            method = request.method
            log_json(
                "info",
                "Incoming request",
                transport="http",
                method=method if method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} else "unknown",
                route=getattr(request.scope.get("route"), "path", "unknown"),
            )

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
