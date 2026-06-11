"""Shared lifecycle for adapters that drive a local CLI through a session manager.

Both the Claude Code (`claude`) and OpenCode (`opencode run`) adapters spawn a
local CLI process per task worktree and stream its output to Agentis via a
``BaseSessionManager``. Neither deploys a long-running web server.

``ClaudeCodeAdapterService`` and ``OpenCodeAdapterService`` are siblings: both
subclass this base and override only the few CLI-specific knobs (fork support
and the label used in logs and skip payloads).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.attachments import (
    attachment_field,
    build_attachments_block,
    decode_attachment_bytes,
    materialize_attachments,
    next_attachment_index,
    safe_attachment_filename,
)
from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.adapter_base import log_json
from common.git_adapter import GitAdapterService

if TYPE_CHECKING:
    from common.session_manager import BaseSessionManager


class CliAdapterService(GitAdapterService):
    """Base adapter for CLI agents run locally.

    Subclasses set :attr:`runtime_label` — used in log messages, the local
    ``wait_ready`` URL and the deploy skip reason — and may override
    :attr:`supports_fork`.
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

    # ------------------------------------------------------------------
    # Deploy / wait_ready — the CLI runs locally, both are no-ops.
    # ------------------------------------------------------------------

    def deploy(self) -> dict[str, Any]:
        log_json(
            "INFO",
            f"Skipping deploy for {self.runtime_label} adapter",
            task_id=self.context.task_id,
        )
        return {
            "action": "deploy",
            "task_id": self.context.task_id,
            "status": "skipped",
            "reason": f"{self.runtime_label}_local",
        }

    def wait_ready(self, timeout: float = 300.0, interval: float = 2.0) -> dict[str, Any]:
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
        return attachment_field(attachment, field_name)

    @staticmethod
    def _safe_attachment_filename(index: int, attachment: Any) -> str:
        return safe_attachment_filename(index, attachment)

    @classmethod
    def _decode_attachment_bytes(cls, attachment: Any) -> bytes | None:
        return decode_attachment_bytes(attachment)

    def _materialize_attachments(
        self,
        worktree: str,
        attachments: list[Any] | None = None,
        *,
        start_index: int = 1,
    ) -> list[dict[str, str]]:
        return materialize_attachments(
            worktree,
            self.context.attachments if attachments is None else attachments,
            task_id=self.context.task_id,
            start_index=start_index,
        )

    @staticmethod
    def _build_attachments_block(attachments: list[dict[str, str]]) -> str | None:
        return build_attachments_block(attachments)

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

        session_id = self._sessions.start(
            context=self.context,
            worktree=working_dir,
            prompt=prompt,
        )
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

    def add_message(
        self,
        message: str,
        pod_url: str | None = None,
        attachments: list[Any] | None = None,
    ) -> dict[str, Any]:
        session_id = self.context.session_id
        if not session_id:
            raise RuntimeError("Context must include session_id to add messages")

        working_dir = str(self._workspace_path())
        prompt = message
        if attachments:
            materialized = self._materialize_attachments(
                working_dir,
                attachments,
                start_index=next_attachment_index(working_dir),
            )
            prompt = self._join_prompt_parts(message, self._build_attachments_block(materialized)) or message
        self._sessions.send(
            session_id=session_id,
            context=self.context,
            worktree=working_dir,
            prompt=prompt,
        )
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


__all__ = ["CliAdapterService"]
