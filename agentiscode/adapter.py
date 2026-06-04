from __future__ import annotations

from typing import cast

from common.models import AgentExecutionContextPayload
from common.config import Settings
from common.cli_adapter import KUBERNETES_MODE, LOCAL_MODE, CliAdapterService
from common.session_manager import BaseSessionManager
from agentiscode.session_manager import AgentisCodeSessionManager


class AgentisCodeAdapterService(CliAdapterService):
    runtime_label = "agentiscode"

    def __init__(
        self,
        context: AgentExecutionContextPayload,
        settings: Settings,
        session_manager: AgentisCodeSessionManager,
    ) -> None:
        super().__init__(context, settings, cast(BaseSessionManager, session_manager))
        runtime = context.adapter.runtime if context.adapter and context.adapter.runtime else None
        if runtime and runtime.strip().lower() == KUBERNETES_MODE:
            self._mode = KUBERNETES_MODE
        else:
            self._mode = LOCAL_MODE

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


__all__ = ["AgentisCodeAdapterService", "KUBERNETES_MODE", "LOCAL_MODE"]
