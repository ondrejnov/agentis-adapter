from __future__ import annotations

from fastapi import FastAPI

from common.adapter_app import JsonRpcRoute, create_adapter_app
from common.config import Settings, get_settings
from common.models import (
    AbortParams,
    AddMessageParams,
    StartParams,
    UndoParams,
)
from common.rpc.jsonrpc import AgentJsonRpcService
from common.rpc.session_registry import SessionContextRegistry
from agentiscode.adapter import AgentisCodeAdapterService
from agentiscode.session_manager import AgentisCodeSessionManager


_DISPATCH: dict[str, JsonRpcRoute] = {
    "start": JsonRpcRoute(StartParams, "start"),
    "add_message": JsonRpcRoute(AddMessageParams, "add_message"),
    "abort": JsonRpcRoute(AbortParams, "abort"),
    "undo": JsonRpcRoute(UndoParams, "undo"),
}


def _configure_services(app: FastAPI, settings: Settings, session_registry: SessionContextRegistry) -> None:
    agentiscode_session_manager = AgentisCodeSessionManager(settings=settings)
    app.state.agentiscode_session_manager = agentiscode_session_manager
    app.state.agent_jsonrpc_service = AgentJsonRpcService(
        settings=settings,
        session_registry=session_registry,
        adapter_factory=lambda context: AgentisCodeAdapterService(
            context=context,
            settings=settings,
            session_manager=agentiscode_session_manager,
        ),
    )


def create_app() -> FastAPI:
    settings = get_settings()
    return create_adapter_app(
        title="Agentis AgentisCode Adapter",
        settings=settings,
        configure_services=_configure_services,
    )


app = create_app()


__all__ = ["_DISPATCH", "app", "create_app"]
