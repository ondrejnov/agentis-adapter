from __future__ import annotations

from typing import Any

from common.adapter_base import log_json
from common.git_adapter import GitAdapterService
from common.models import AgentExecutionContextPayload
from common.config import Settings
from agentiscode.session_manager import AgentisCodeSessionManager


class AgentisCodeAdapterService(GitAdapterService):
    runtime_label = "agentiscode"

    def __init__(
        self,
        context: AgentExecutionContextPayload,
        settings: Settings,
        session_manager: AgentisCodeSessionManager,
    ) -> None:
        super().__init__(context, settings)
        self._sessions = session_manager

    def deploy(self) -> dict[str, Any]:
        log_json("INFO", "Skipping Kubernetes deploy for agentiscode adapter", task_id=self.context.task_id)
        return {
            "action": "deploy",
            "task_id": self.context.task_id,
            "status": "skipped",
            "reason": "agentiscode_local",
        }

    def wait_ready(self, timeout: float = 300.0, interval: float = 2.0) -> dict[str, Any]:
        return {
            "action": "wait_ready",
            "task_id": self.context.task_id,
            "url": "local://agentiscode",
            "status": "skipped",
        }

    @staticmethod
    def _join_prompt_parts(*texts: str | None) -> str:
        chunks: list[str] = []
        for text in texts:
            if not isinstance(text, str):
                continue
            stripped = text.strip()
            if stripped and (not chunks or chunks[-1] != stripped):
                chunks.append(stripped)
        return "\n\n".join(chunks)

    def _build_initial_prompt(self) -> str:
        prompt = self._join_prompt_parts(self.context.user_prompt, self.context.description)
        return prompt or self.context.title.strip()

    def start_session(self, pod_url: str | None = None, fork_from_session_id: str | None = None) -> dict[str, Any]:
        if fork_from_session_id:
            raise RuntimeError("agentiscode adapter nepodporuje fork_from_session_id.")
        working_dir = str(self._workspace_path())
        prompt = self._build_initial_prompt()
        if not prompt:
            raise RuntimeError("Cannot start agentiscode session without a prompt")
        session_id = self._sessions.start(context=self.context, worktree=working_dir, prompt=prompt)
        self.context.session_id = session_id
        try:
            self._persist_agentis_session_id(session_id)
        except RuntimeError as exc:
            log_json("WARN", "Failed to persist agentiscode session in Agentis", error=str(exc))
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
        self._sessions.send(
            session_id=session_id,
            context=self.context,
            worktree=str(self._workspace_path()),
            prompt=message,
        )
        return {
            "action": "add_message",
            "task_id": self.context.task_id,
            "session_id": session_id,
            "snapshot_key": self._sessions.get_snapshot_key(session_id),
        }

    def abort(self, session_id: str) -> dict[str, Any]:
        self._sessions.abort(session_id)
        return {"action": "abort", "task_id": self.context.task_id, "session_id": session_id}

    def close(self) -> dict[str, Any]:
        if self.context.session_id:
            self._sessions.abort(self.context.session_id)
            self._sessions.remove(self.context.session_id)
        return super().close()


__all__ = ["AgentisCodeAdapterService"]
