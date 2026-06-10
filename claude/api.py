from __future__ import annotations

from fastapi import FastAPI

from common.adapter_app import JsonRpcRoute, create_adapter_app
from common.config import Settings, get_settings
from common.models import (
    AddMessageParams,
    AbortParams,
    ApproveParams,
    QuestionParams,
    StartParams,
    UndoParams,
)
from claude.adapter import ClaudeCodeAdapterService
from claude.session_manager import ClaudeSessionManager
from common.rpc.jsonrpc import AgentJsonRpcService
from common.rpc.session_registry import SessionContextRegistry


_DISPATCH: dict[str, JsonRpcRoute] = {
    "start": JsonRpcRoute(StartParams, "start"),
    "add_message": JsonRpcRoute(AddMessageParams, "add_message"),
    "question": JsonRpcRoute(QuestionParams, "question"),
    "approve": JsonRpcRoute(ApproveParams, "approve"),
    "abort": JsonRpcRoute(AbortParams, "abort"),
    "undo": JsonRpcRoute(UndoParams, "undo"),
}


def _configure_services(app: FastAPI, settings: Settings, session_registry: SessionContextRegistry) -> None:
    claude_session_manager = ClaudeSessionManager(settings=settings)
    app.state.claude_session_manager = claude_session_manager
    app.state.agent_jsonrpc_service = AgentJsonRpcService(
        settings=settings,
        session_registry=session_registry,
        adapter_factory=lambda context: ClaudeCodeAdapterService(
            context=context,
            settings=settings,
            session_manager=claude_session_manager,
        ),
    )


def create_app() -> FastAPI:
    settings = get_settings()
    return create_adapter_app(
        title="Agentis ClaudeCode Adapter",
        settings=settings,
        configure_services=_configure_services,
    )


app = create_app()
