from __future__ import annotations

from fastapi import FastAPI

from common.adapter_app import JsonRpcRoute, create_adapter_app
from common.config import Settings, get_settings
from common.models import (
    AddMessageParams,
    AbortParams,
    StartParams,
    UndoParams,
)
from claude_p.adapter import ClaudePAdapterService
from claude_p.session_manager import ClaudePSessionManager
from common.rpc.jsonrpc import AgentJsonRpcService
from common.rpc.session_registry import SessionContextRegistry


_DISPATCH: dict[str, JsonRpcRoute] = {
    "start": JsonRpcRoute(StartParams, "start"),
    "add_message": JsonRpcRoute(AddMessageParams, "add_message"),
    "abort": JsonRpcRoute(AbortParams, "abort"),
    "undo": JsonRpcRoute(UndoParams, "undo"),
}


def _configure_services(app: FastAPI, settings: Settings, session_registry: SessionContextRegistry) -> None:
    claude_p_session_manager = ClaudePSessionManager(settings=settings)
    app.state.claude_p_session_manager = claude_p_session_manager
    app.state.agent_jsonrpc_service = AgentJsonRpcService(
        settings=settings,
        session_registry=session_registry,
        adapter_factory=lambda context: ClaudePAdapterService(
            context=context,
            settings=settings,
            session_manager=claude_p_session_manager,
        ),
    )


def create_app() -> FastAPI:
    settings = get_settings()
    return create_adapter_app(
        title="Agentis ClaudeP Adapter",
        settings=settings,
        configure_services=_configure_services,
    )


app = create_app()
