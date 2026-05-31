"""Shared lifecycle for adapters that drive a local CLI through a session manager.

Both the Claude Code (`claude`) and OpenCode (`opencode run`) adapters spawn a
local CLI process per task worktree and stream its output to Agentis via a
``BaseSessionManager``. Neither deploys a long-running web server; the only
Kubernetes machinery they need is the optional ``kubernetes`` fallback, where
the regular Kubernetes runtime is delegated to and the CLI is invoked through
``kubectl exec``.

``ClaudeCodeAdapterService`` and ``OpenCodeAdapterService`` are siblings: both
subclass this base and override only the few CLI-specific knobs (the run-mode
default, fork support, and the label used in logs and skip payloads).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.cli_session import KubectlExecTarget
from common.adapter_base import log_json
from common.git_adapter import GitAdapterService
from common.kubernetes.runtime import KubernetesRuntime

if TYPE_CHECKING:
    from common.session_manager import BaseSessionManager


KUBERNETES_MODE = "kubernetes"
LOCAL_MODE = "local"


class CliAdapterService(GitAdapterService):
    """Base adapter for CLI agents run locally (or via ``kubectl exec``).

    Subclasses set :attr:`runtime_label` — used in log messages, the local
    ``wait_ready`` URL and the deploy skip reason — and may override
    :meth:`_default_run_mode` or :attr:`supports_fork`.
    """

    requires_agentis_init = False

    #: Identifier of the CLI; drives skip reasons, log messages and the local URL.
    runtime_label = "cli"
    #: Whether the backing session manager can fork from an existing session.
    supports_fork = False

    def __init__(
        self,
        context: AgentExecutionContextPayload,
        settings: Settings,
        session_manager: "BaseSessionManager",
    ) -> None:
        super().__init__(context, settings)
        self._sessions = session_manager
        runtime = context.adapter.runtime if context.adapter and context.adapter.runtime else None
        self._mode = (runtime or self._default_run_mode() or LOCAL_MODE).lower()

    # ------------------------------------------------------------------
    # CLI-specific hooks
    # ------------------------------------------------------------------

    def _default_run_mode(self) -> str | None:
        """Run mode used when the context does not pin one. Defaults to ``local``."""
        return LOCAL_MODE

    @property
    def is_kubernetes_mode(self) -> bool:
        return self._mode == KUBERNETES_MODE

    def _kubectl_target(self) -> KubectlExecTarget:
        namespace = KubernetesRuntime.namespace_for_context(self.context, self.settings)
        return KubectlExecTarget(
            namespace=namespace,
            selector=self.settings.claude_pod_selector,
            container=self.settings.claude_pod_container,
            kubectl=self.settings.kubectl_command,
        )

    def _kubernetes_runtime(self) -> KubernetesRuntime:
        return KubernetesRuntime(self.context, self.settings, self._workspace_path())

    # ------------------------------------------------------------------
    # Deploy / wait_ready — no-op locally, reuse the k8s flow in kubernetes mode.
    # ------------------------------------------------------------------

    def deploy(self) -> dict[str, Any]:
        if self.is_kubernetes_mode:
            return self._kubernetes_runtime().deploy()
        log_json(
            "INFO",
            f"Skipping Kubernetes deploy for {self.runtime_label} adapter",
            task_id=self.context.task_id,
        )
        return {
            "action": "deploy",
            "task_id": self.context.task_id,
            "status": "skipped",
            "reason": f"{self.runtime_label}_local",
        }

    def wait_ready(self, timeout: float = 300.0, interval: float = 2.0) -> dict[str, Any]:
        if self.is_kubernetes_mode:
            return self._kubernetes_runtime().wait_ready(timeout=timeout, interval=interval)
        return {
            "action": "wait_ready",
            "task_id": self.context.task_id,
            "url": f"local://{self.runtime_label}",
            "status": "skipped",
        }

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _comment_field(comment: Any, field_name: str) -> Any:
        if isinstance(comment, dict):
            return comment.get(field_name)
        return getattr(comment, field_name, None)

    @classmethod
    def _build_comments_block(cls, comments: list[Any] | None) -> str | None:
        if not comments:
            return None

        entries: list[str] = []
        for index, comment in enumerate(comments, start=1):
            body = cls._comment_field(comment, "body")
            if not isinstance(body, str) or not body.strip():
                continue

            author_name = cls._comment_field(comment, "author_name")
            author_type = cls._comment_field(comment, "author_type")
            created = cls._comment_field(comment, "created")

            meta_parts: list[str] = []
            if isinstance(author_name, str) and author_name.strip():
                meta_parts.append(author_name.strip())
            elif isinstance(author_type, str) and author_type.strip():
                meta_parts.append(author_type.strip())

            if isinstance(created, str) and created.strip():
                meta_parts.append(created.strip())

            header = f"{index}."
            if meta_parts:
                header = f"{header} {' | '.join(meta_parts)}"

            entries.append(f"{header}\n{body.strip()}")

        if not entries:
            return None

        return "<comments>\n" + "\n\n".join(entries) + "\n</comments>"

    @staticmethod
    def _join_prompt_parts(*texts: str | None) -> str:
        chunks: list[str] = []
        for text in texts:
            if not isinstance(text, str):
                continue
            stripped = text.strip()
            if not stripped:
                continue
            if chunks and chunks[-1] == stripped:
                continue
            chunks.append(stripped)
        return "\n\n".join(chunks)

    def _build_initial_prompt(self) -> str:
        comments_block = self._build_comments_block(self.context.comments)
        prompt = self._join_prompt_parts(self.context.user_prompt, self.context.description, comments_block)
        if prompt:
            return prompt
        return self._join_prompt_parts(self.context.title, comments_block)

    def start_session(self, pod_url: str | None = None, fork_from_session_id: str | None = None) -> dict[str, Any]:
        if fork_from_session_id and not self.supports_fork:
            raise RuntimeError(f"{self.runtime_label} adapter nepodporuje fork_from_session_id.")
        working_dir = str(self._workspace_path())
        prompt = self._build_initial_prompt()
        if not prompt:
            raise RuntimeError(f"Cannot start {self.runtime_label} session without a prompt")

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
            f"{self.runtime_label} session created",
            task_id=self.context.task_id,
            session_id=session_id,
        )
        try:
            self._persist_agentis_session_id(session_id)
        except RuntimeError as exc:
            log_json(
                "WARN",
                f"Failed to persist {self.runtime_label} session in Agentis",
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

    def abort(self, session_id: str) -> dict[str, Any]:
        self._sessions.abort(session_id)
        log_json(
            "INFO",
            f"{self.runtime_label} session aborted",
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

        log_json(
            "INFO",
            f"Closing {self.runtime_label} task environment",
            task_id=self.context.task_id,
        )

        if self.is_kubernetes_mode:
            kubernetes_teardown = self._kubernetes_runtime().teardown()
            return {**super().close(), **kubernetes_teardown}

        return super().close()


__all__ = ["CliAdapterService", "KUBERNETES_MODE", "LOCAL_MODE"]
