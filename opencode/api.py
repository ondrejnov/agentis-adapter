from __future__ import annotations

from fastapi import FastAPI

from common.adapter_app import JsonRpcRoute, create_adapter_app
from common.config import Settings, get_settings
from common.models import (
    AbortParams,
    AddMessageParams,
    AddQuestionParams,
    ApproveParams,
    CloseParams,
    GitMergeParams,
    ProviderSyncUsageParams,
    QuestionParams,
    SessionCreatedParams,
    SessionErrorParams,
    SessionIdleParams,
    SessionUpdateParams,
    StartParams,
    StartTaskParams,
    StoreActivityLogParams,
)
from common.rpc.internal import InternalRpcService
from opencode.adapter import OpenCodeAdapterService
from opencode.session_manager import OpenCodeSessionManager
from common.rpc.jsonrpc import AgentJsonRpcService
from common.rpc.session_registry import SessionContextRegistry
from common.usage.provider import ProviderUsageSyncService


_DISPATCH: dict[str, JsonRpcRoute] = {
    "start": JsonRpcRoute(StartParams, "start"),
    "add_message": JsonRpcRoute(AddMessageParams, "add_message"),
    "question": JsonRpcRoute(QuestionParams, "question"),
    "approve": JsonRpcRoute(ApproveParams, "approve"),
    "git_merge": JsonRpcRoute(GitMergeParams, "git_merge"),
    "abort": JsonRpcRoute(AbortParams, "abort"),
    "close": JsonRpcRoute(CloseParams, "close"),
    "provider.sync_usage": JsonRpcRoute(
        ProviderSyncUsageParams,
        "sync_provider_usage",
        service_attr="provider_usage_sync_service",
    ),
}

_INTERNAL_DISPATCH: dict[str, JsonRpcRoute] = {
    "session.start_task": JsonRpcRoute(StartTaskParams, "start_task", service_attr="internal_rpc_service"),
    "session.session_idle": JsonRpcRoute(SessionIdleParams, "session_idle", service_attr="internal_rpc_service"),
    "session.session_update": JsonRpcRoute(SessionUpdateParams, "session_update", service_attr="internal_rpc_service"),
    "session.session_error": JsonRpcRoute(SessionErrorParams, "session_error", service_attr="internal_rpc_service"),
    "session.session_created": JsonRpcRoute(SessionCreatedParams, "session_created", service_attr="internal_rpc_service"),
    "session.store_activity_log": JsonRpcRoute(
        StoreActivityLogParams,
        "store_activity_log",
        service_attr="internal_rpc_service",
    ),
    "task.add_question": JsonRpcRoute(AddQuestionParams, "add_question", service_attr="internal_rpc_service"),
}


def _configure_services(app: FastAPI, settings: Settings, session_registry: SessionContextRegistry) -> None:
    opencode_session_manager = OpenCodeSessionManager(settings=settings)
    app.state.opencode_session_manager = opencode_session_manager
    app.state.agent_jsonrpc_service = AgentJsonRpcService(
        settings=settings,
        session_registry=session_registry,
        adapter_factory=lambda context: OpenCodeAdapterService(
            context=context,
            settings=settings,
            session_manager=opencode_session_manager,
        ),
    )
    app.state.provider_usage_sync_service = ProviderUsageSyncService(settings=settings)
    app.state.internal_rpc_service = InternalRpcService(settings=settings, session_registry=session_registry)


def create_app() -> FastAPI:
    settings = get_settings()
    return create_adapter_app(
        title="Agentis OpenCode Adapter",
        settings=settings,
        configure_services=_configure_services,
        dispatch=_DISPATCH,
        internal_dispatch=_INTERNAL_DISPATCH,
    )


app = create_app()
