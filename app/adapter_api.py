"""Single generic serving adapter.

Run lifecycle (agent execution, commit, PR) běží přes workflow runtime; serving
adapter poskytuje jen git worktree/snapshot plumbing z
:class:`~common.git_adapter.GitAdapterService`. Konkrétní CLI agent (opencode /
claude / claude-p) se vybírá až v workflow kroku (`agentiscode --adapter …`), ne
tady — proto stačí jeden generický serving adapter místo per-agent variant.
"""

from __future__ import annotations

from fastapi import FastAPI

from common.adapter_app import JsonRpcRoute, create_adapter_app
from common.config import Settings, get_settings
from common.git_adapter import GitAdapterService
from common.models import (
    AbortParams,
    AddMessageParams,
    StartParams,
    UndoParams,
)
from common.rpc.jsonrpc import AgentJsonRpcService
from common.rpc.session_registry import SessionContextRegistry


_DISPATCH: dict[str, JsonRpcRoute] = {
    "start": JsonRpcRoute(StartParams, "start"),
    "add_message": JsonRpcRoute(AddMessageParams, "add_message"),
    "abort": JsonRpcRoute(AbortParams, "abort"),
    "undo": JsonRpcRoute(UndoParams, "undo"),
}


def _configure_services(app: FastAPI, settings: Settings, session_registry: SessionContextRegistry) -> None:
    app.state.agent_jsonrpc_service = AgentJsonRpcService(
        settings=settings,
        session_registry=session_registry,
        adapter_factory=lambda context: GitAdapterService(context=context, settings=settings),
    )


def create_app() -> FastAPI:
    settings = get_settings()
    return create_adapter_app(
        title="Agentis Adapter",
        settings=settings,
        configure_services=_configure_services,
    )


app = create_app()


__all__ = ["_DISPATCH", "app", "create_app"]
