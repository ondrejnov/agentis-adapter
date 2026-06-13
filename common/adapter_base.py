"""Minimal adapter base.

``BaseAdapterService`` is intentionally tiny: it accepts an execution context,
talks to Agentis (progress events + session persistence) and declares the agent
lifecycle that concrete adapters implement. It deliberately knows nothing about
git or worktrees — those concerns live in :class:`GitAdapterService`.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.status import get_status_registry


def log_json(level: str, message: str, **fields) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
        **fields,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    get_status_registry().log(level, message, fields)


class BaseAdapterService:
    """Accept a context, run an agent, report progress/results back to Agentis."""

    requires_agentis_init = False

    def __init__(self, context: AgentExecutionContextPayload, settings: Settings):
        self.context = context
        self.settings = settings
        print(f"Adapter initialized with context: {self.context}")

    @staticmethod
    def is_project_scope(context: AgentExecutionContextPayload) -> bool:
        return bool(context.adapter and context.adapter.scope == "project")

    # ------------------------------------------------------------------
    # Agentis reporting
    # ------------------------------------------------------------------

    def _agentis_client_class(self) -> Any:
        return AgentisJsonRpcClient

    def _call_agentis_rpc(self, method: str, params: dict[str, Any], *, timeout: float = 10.0) -> Any:
        endpoint = self.settings.agentis_endpoint
        if not endpoint:
            raise RuntimeError("agentis_endpoint is not configured")

        try:
            with self._agentis_client_class()(
                endpoint=endpoint,
                token=self.settings.agentis_token,
                timeout=timeout,
            ) as client:
                return client.call(method=method, params=params, request_id=1)
        except AgentisJsonRpcError as exc:
            raise RuntimeError(str(exc)) from exc

    def post_agentis_event(
        self,
        *,
        kind: str,
        status: str,
        event_id: str | None = None,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if not self.settings.agentis_endpoint:
            log_json(
                "WARN",
                "Agentis endpoint missing; skipping adapter event",
                task_id=self.context.task_id,
                run_id=self.context.run_id,
                kind=kind,
                status=status,
            )
            return

        normalized_event_id = event_id or f"{kind}:{uuid4().hex}"
        payload = {
            "run_id": self.context.run_id,
            "kind": kind,
            "status": status,
            "event_id": normalized_event_id,
            "message": message,
            "data": data or {},
        }
        log_json(
            "INFO",
            "Posting adapter event to Agentis",
            task_id=self.context.task_id,
            run_id=self.context.run_id,
            kind=kind,
            status=status,
            event_id=normalized_event_id,
            event_message=message,
        )
        try:
            self._call_agentis_rpc("run.adapter_event", payload)
        except Exception as exc:
            print(f"Failed to post adapter event to Agentis: {exc}", file=sys.stderr)
            log_json(
                "WARN",
                "Failed to post adapter event to Agentis",
                task_id=self.context.task_id,
                run_id=self.context.run_id,
                kind=kind,
                status=status,
                event_id=normalized_event_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Worktree lifecycle — implemented by GitAdapterService
    # ------------------------------------------------------------------

    def create_worktree(self) -> dict[str, Any]:
        raise NotImplementedError


__all__ = ["BaseAdapterService", "log_json"]
