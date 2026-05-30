"""Adapter that runs Claude Code locally instead of deploying OpenCode to k8s.

Reuse shared git/worktree/session plumbing from the adapter base and override
only runtime-specific steps (deploy, wait_ready, start_session, add_message,
abort, close).
"""

from __future__ import annotations

from typing import Any

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.adapter_base import log_json
from common.kubernetes_runtime import KubernetesAdapterService
from claude.session_manager import ClaudeSessionManager
from claude.client import KubectlExecTarget
from opencode.utils import OpenCodeUtils


KUBERNETES_MODE = "kubernetes"
LOCAL_MODE = "local"


class ClaudeCodeAdapterService(KubernetesAdapterService):
    """Variant adapter that runs the local `claude` CLI for the task worktree."""

    requires_agentis_init = False

    def __init__(
        self,
        context: AgentExecutionContextPayload,
        settings: Settings,
        session_manager: ClaudeSessionManager,
    ) -> None:
        super().__init__(context, settings)
        self._sessions = session_manager
        runtime = context.adapter.runtime if context.adapter and context.adapter.runtime else None
        self._mode = (runtime or settings.claude_run_mode or LOCAL_MODE).lower()

    @property
    def is_kubernetes_mode(self) -> bool:
        return self._mode == KUBERNETES_MODE

    def _kubectl_target(self) -> KubectlExecTarget:
        namespace = self.namespace_for_context(self.context, self.settings)
        return KubectlExecTarget(
            namespace=namespace,
            selector=self.settings.claude_pod_selector,
            container=self.settings.claude_pod_container,
            kubectl=self.settings.kubectl_command,
        )

    # ------------------------------------------------------------------
    # Deploy / wait_ready — v `local` módu jsou no-op, v `kubernetes` módu
    # použijeme stejný flow jako opencode adapter (pod běží stejně, jen
    # vstupní claude se volá přes `kubectl exec`).
    # ------------------------------------------------------------------

    def deploy(self) -> dict[str, Any]:
        if self.is_kubernetes_mode:
            return super().deploy()
        log_json(
            "INFO",
            "Skipping Kubernetes deploy for ClaudeCode adapter",
            task_id=self.context.task_id,
        )
        return {
            "action": "deploy",
            "task_id": self.context.task_id,
            "status": "skipped",
            "reason": "claude_local",
        }

    def wait_ready(self, timeout: float = 300.0, interval: float = 2.0) -> dict[str, Any]:
        if self.is_kubernetes_mode:
            return super().wait_ready(timeout=timeout, interval=interval)
        # ClaudeCode běží lokálně — žádný pod k čekání není.
        return {
            "action": "wait_ready",
            "task_id": self.context.task_id,
            "url": "local://claude",
            "status": "skipped",
        }

    # ------------------------------------------------------------------
    # Claude session lifecycle
    # ------------------------------------------------------------------

    def _build_initial_prompt(self) -> str:
        comments_block = OpenCodeUtils.build_comments_block(self.context.comments)
        parts = OpenCodeUtils.build_text_parts(self.context.user_prompt, self.context.description, comments_block)
        if not parts:
            parts = OpenCodeUtils.build_text_parts(self.context.title, comments_block)
        if parts:
            return parts[0]["text"]
        return self.context.title or ""

    def start_session(self, pod_url: str | None = None, fork_from_session_id: str | None = None) -> dict[str, Any]:
        if fork_from_session_id:
            raise RuntimeError("Claude adapter nepodporuje fork_from_session_id.")
        working_dir = str(self._workspace_path())
        prompt = self._build_initial_prompt()
        print(prompt)
        if not prompt:
            raise RuntimeError("Cannot start claude session without a prompt")

        start_kwargs: dict[str, Any] = {
            "context": self.context,
            "worktree": working_dir,
            "prompt": prompt,
        }
        if self.is_kubernetes_mode:
            start_kwargs["kubectl_target"] = self._kubectl_target()
        session_id = self._sessions.start(**start_kwargs)
        self.context.session_id = session_id

        log_json(
            "INFO",
            "Claude session created",
            task_id=self.context.task_id,
            session_id=session_id,
        )
        try:
            self._persist_agentis_session_id(session_id)
        except RuntimeError as exc:
            log_json(
                "WARN",
                "Failed to persist claude session in Agentis",
                task_id=self.context.task_id,
                session_id=session_id,
                error=str(exc),
            )

        return {
            "action": "start_session",
            "task_id": self.context.task_id,
            "session_id": session_id,
        }

    def add_message(self, message: str, pod_url: str | None = None) -> dict[str, Any]:
        session_id = self.context.session_id
        if not session_id:
            raise RuntimeError("Context must include session_id to add messages")

        working_dir = str(self._workspace_path())
        send_kwargs: dict[str, Any] = {
            "session_id": session_id,
            "context": self.context,
            "worktree": working_dir,
            "prompt": message,
        }
        if self.is_kubernetes_mode:
            send_kwargs["kubectl_target"] = self._kubectl_target()
        self._sessions.send(**send_kwargs)
        return {
            "action": "add_message",
            "task_id": self.context.task_id,
            "session_id": session_id,
        }

    def question_reply(self, request_id: str, answers: list[list[str]], pod_url: str | None = None):
        return None

    def abort(self, session_id: str) -> dict[str, Any]:
        self._sessions.abort(session_id)
        log_json(
            "INFO",
            "Claude session aborted",
            task_id=self.context.task_id,
            session_id=session_id,
        )
        return {
            "action": "abort",
            "task_id": self.context.task_id,
            "session_id": session_id,
        }

    # ------------------------------------------------------------------
    # Tear-down
    # ------------------------------------------------------------------

    def close(self) -> dict[str, Any]:
        if self.context.session_id:
            self._sessions.abort(self.context.session_id)
            self._sessions.remove(self.context.session_id)

        if self.is_kubernetes_mode:
            return super().close()

        repository_root = self._repository_root()
        branch_name = self._branch_name_for_context(self.context)
        worktree_path = self._resolved_worktree_path()

        log_json(
            "INFO",
            "Closing claude task environment",
            task_id=self.context.task_id,
            branch=branch_name,
            worktree_path=str(worktree_path),
        )

        worktree_removed, branch_deleted = self._cleanup_worktree_branch(
            repository_root,
            branch_name,
            worktree_path,
            missing_worktree_is_removed=True,
        )

        return {
            "action": "close",
            "task_id": self.context.task_id,
            "branch": branch_name,
            "base_branch": self.context.base_branch,
            "worktree_path": str(worktree_path),
            "worktree_removed": worktree_removed,
            "branch_deleted": branch_deleted,
        }
