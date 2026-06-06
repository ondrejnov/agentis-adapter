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

import base64
import binascii
import mimetypes
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.cli_session import KubectlExecTarget
from common.adapter_base import log_json
from common.git_adapter import GitAdapterService
from common.kubernetes.ci_workflow import CiStep
from common.kubernetes.runtime import KubernetesRuntime

if TYPE_CHECKING:
    from common.session_manager import BaseSessionManager


KUBERNETES_MODE = "kubernetes"
LOCAL_MODE = "local"
_ATTACHMENTS_DIR = Path(".agentis/attachments")
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


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
    # CI setup workflow — runs in kubernetes mode only (local needs no setup).
    # ------------------------------------------------------------------

    def ci_setup_steps(self) -> list[CiStep]:
        if self.is_kubernetes_mode:
            return self._kubernetes_runtime().ci_setup_steps()
        return []

    def run_ci_step(self, step: CiStep) -> dict[str, Any]:
        return self._kubernetes_runtime().run_ci_step(step)

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

    @staticmethod
    def _attachment_field(attachment: Any, field_name: str) -> Any:
        if isinstance(attachment, dict):
            return attachment.get(field_name)
        return getattr(attachment, field_name, None)

    @staticmethod
    def _safe_attachment_filename(index: int, attachment: Any) -> str:
        raw_filename = CliAdapterService._attachment_field(attachment, "filename")
        raw_path = CliAdapterService._attachment_field(attachment, "path")
        raw_mime = CliAdapterService._attachment_field(attachment, "mime")

        filename = raw_filename if isinstance(raw_filename, str) and raw_filename.strip() else None
        if filename is None and isinstance(raw_path, str) and raw_path.strip():
            filename = Path(raw_path).name
        if filename is None:
            filename = f"attachment-{index}"

        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).name).strip(".-")
        if not safe_name:
            safe_name = f"attachment-{index}"

        mime = raw_mime.strip().lower() if isinstance(raw_mime, str) and raw_mime.strip() else ""
        if "." not in safe_name and mime in _MIME_EXTENSIONS:
            safe_name = f"{safe_name}{_MIME_EXTENSIONS[mime]}"

        return f"{index:03d}-{safe_name}"

    @classmethod
    def _decode_attachment_bytes(cls, attachment: Any) -> bytes | None:
        content_base64 = cls._attachment_field(attachment, "content_base64")
        if isinstance(content_base64, str) and content_base64.strip():
            try:
                return base64.b64decode(content_base64.strip(), validate=True)
            except (binascii.Error, ValueError):
                pass

        raw_path = cls._attachment_field(attachment, "path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None

        file_path = Path(raw_path)
        if not file_path.exists() or not file_path.is_file():
            return None

        try:
            return file_path.read_bytes()
        except OSError:
            return None

    def _exclude_attachment_dir_from_git(self, worktree_path: Path) -> None:
        if not self._git_succeeds(worktree_path, "rev-parse", "--is-inside-work-tree"):
            return

        try:
            exclude_path = Path(self._run_git(worktree_path, "rev-parse", "--git-path", "info/exclude"))
            if not exclude_path.is_absolute():
                exclude_path = worktree_path / exclude_path
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
            pattern = f"/{_ATTACHMENTS_DIR.as_posix()}/"
            if pattern not in {line.strip() for line in existing.splitlines()}:
                with exclude_path.open("a", encoding="utf-8") as handle:
                    if existing and not existing.endswith("\n"):
                        handle.write("\n")
                    handle.write(f"{pattern}\n")
        except Exception as exc:  # noqa: BLE001
            log_json(
                "WARN",
                "Failed to exclude CLI attachments from git",
                task_id=self.context.task_id,
                error=str(exc),
            )

    def _materialize_attachments(self, worktree: str) -> list[dict[str, str]]:
        if not self.context.attachments:
            return []

        worktree_path = Path(worktree)
        target_dir = worktree_path / _ATTACHMENTS_DIR
        materialized: list[dict[str, str]] = []

        for index, attachment in enumerate(self.context.attachments, start=1):
            data = self._decode_attachment_bytes(attachment)
            if data is None:
                continue

            target_dir.mkdir(parents=True, exist_ok=True)
            filename = self._safe_attachment_filename(index, attachment)
            target_path = target_dir / filename
            try:
                target_path.write_bytes(data)
            except OSError:
                continue

            raw_mime = self._attachment_field(attachment, "mime")
            mime = raw_mime.strip() if isinstance(raw_mime, str) and raw_mime.strip() else None
            if mime is None:
                mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            materialized.append(
                {
                    "filename": filename,
                    "mime": mime,
                    "path": str(target_path.relative_to(worktree_path)),
                }
            )

        if materialized:
            self._exclude_attachment_dir_from_git(worktree_path)

        return materialized

    @staticmethod
    def _build_attachments_block(attachments: list[dict[str, str]]) -> str | None:
        if not attachments:
            return None

        lines = [
            "<attachments>",
            "Agentis attachments were saved as local files. Use these paths when relevant; inspect image files from disk.",
        ]
        for index, attachment in enumerate(attachments, start=1):
            mime = attachment["mime"]
            kind = "image" if mime.startswith("image/") else "file"
            lines.append(f"{index}. {kind}: {attachment['filename']}")
            lines.append(f"path: {attachment['path']}")
            lines.append(f"mime: {mime}")
        lines.append("</attachments>")
        return "\n".join(lines)

    def _build_initial_prompt(self, worktree: str | None = None) -> str:
        comments_block = self._build_comments_block(self.context.comments)
        attachments_block = self._build_attachments_block(self._materialize_attachments(worktree) if worktree else [])
        prompt = self._join_prompt_parts(
            self.context.user_prompt,
            self.context.description,
            comments_block,
            attachments_block,
        )
        if prompt:
            return prompt
        return self._join_prompt_parts(self.context.title, comments_block, attachments_block)

    def start_session(self, pod_url: str | None = None, fork_from_session_id: str | None = None) -> dict[str, Any]:
        if fork_from_session_id and not self.supports_fork:
            raise RuntimeError(f"{self.runtime_label} adapter nepodporuje fork_from_session_id.")
        working_dir = str(self._workspace_path())
        prompt = self._build_initial_prompt(working_dir)
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
            "snapshot_key": self._sessions.get_snapshot_key(session_id),
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
            "snapshot_key": self._sessions.get_snapshot_key(session_id),
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
