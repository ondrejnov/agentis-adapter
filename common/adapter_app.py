from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI

from common.config import Settings
from common.rpc.dispatcher import JsonRpcRoute
from common.rpc.session_registry import SessionContextRegistry


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
    ``claude`` CLI), so this app exposes no inbound RPC port — it only holds the
    configured services on ``app.state`` and serves a ``/health`` liveness probe.
    """
    app = FastAPI(title=title, version=version)
    session_registry = SessionContextRegistry()
    app.state.session_registry = session_registry
    configure_services(app, settings, session_registry)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
