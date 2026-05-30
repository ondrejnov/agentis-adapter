"""Adapter that runs OpenCode through `opencode run`.

Na rozdíl od ``KubernetesAdapterService`` nedeployuje OpenCode web server ani
nepoužívá REST/interní API — OpenCode se spustí na jedno zadání promptu
a jeho streamovaný výstup forwardujeme do Agentisu (viz
``OpenCodeSessionManager``). Lifecycle je shodný s Claude Code adaptérem,
proto z něj dědíme a měníme jen runtime-specifické kroky.
"""

from __future__ import annotations

from typing import Any

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.adapter_base import log_json
from claude.adapter import KUBERNETES_MODE, LOCAL_MODE, ClaudeCodeAdapterService
from opencode.session_manager import OpenCodeSessionManager


class OpenCodeAdapterService(ClaudeCodeAdapterService):
    """Variant adapter that runs local `opencode run` for the task worktree."""

    def __init__(
        self,
        context: AgentExecutionContextPayload,
        settings: Settings,
        session_manager: OpenCodeSessionManager,
    ) -> None:
        super().__init__(context, settings, session_manager)
        # OpenCode běží defaultně lokálně; `claude_run_mode` se zde nepoužívá.
        runtime = context.adapter.runtime if context.adapter and context.adapter.runtime else None
        self._mode = (runtime or LOCAL_MODE).lower()

    def deploy(self) -> dict[str, Any]:
        if self.is_kubernetes_mode:
            return super().deploy()
        log_json(
            "INFO",
            "Skipping Kubernetes deploy for OpenCode adapter",
            task_id=self.context.task_id,
        )
        return {
            "action": "deploy",
            "task_id": self.context.task_id,
            "status": "skipped",
            "reason": "opencode_local",
        }

    def wait_ready(self, timeout: float = 300.0, interval: float = 2.0) -> dict[str, Any]:
        if self.is_kubernetes_mode:
            return super().wait_ready(timeout=timeout, interval=interval)
        return {
            "action": "wait_ready",
            "task_id": self.context.task_id,
            "url": "local://opencode",
            "status": "skipped",
        }


__all__ = ["OpenCodeAdapterService", "KUBERNETES_MODE", "LOCAL_MODE"]
