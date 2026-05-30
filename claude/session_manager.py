"""Background runner for local `claude` sessions.

Pro každou ClaudeCode session držíme jeden řídicí thread, který asynchronně
streamuje výstup z `claude` CLI a postupně forwarduje aktivitu do Agentisu
(`session.store_activity_log`, `task.add_agent_comment`, `run.adapter_event`).

Veškerá orchestrace (streaming, activity-log forwarding, dokončovací akce) se
dědí z ``BaseSessionManager``; přepisujeme jen agentně specifické hooky.
"""

from __future__ import annotations

from typing import Optional

from common.session_manager import BaseSessionManager, _AgentSession
from claude.activity_mapper import ClaudeActivityMapper
from claude.client import ClaudeCodeClient, ClaudeRunConfig, KubectlExecTarget

# Backwards-compatible alias — the session dataclass is now agent-agnostic.
_ClaudeSession = _AgentSession

__all__ = [
    "ClaudeSessionManager",
    "_AgentSession",
    "_ClaudeSession",
    "ClaudeCodeClient",
    "ClaudeRunConfig",
    "KubectlExecTarget",
]


class ClaudeSessionManager(BaseSessionManager):
    """Owns background `claude` runs keyed by the real Claude session_id."""

    _AGENT_LABEL = "claude"
    _REMOTE_PKILL_PATTERN = "claude --print"

    def _make_mapper(
        self,
        *,
        prompt: str,
        mode: str,
        cwd: str,
        session_id_hint: Optional[str] = None,
    ) -> ClaudeActivityMapper:
        return ClaudeActivityMapper(
            prompt=prompt,
            mode=mode,
            agent="claude",
            cwd=cwd,
            session_id_hint=session_id_hint,
        )

    def _build_client(self, sess: _AgentSession, resume_id: Optional[str]) -> ClaudeCodeClient:
        adapter_opts = sess.context.adapter
        env: dict[str, str] = {"IS_SANDBOX": "1"}
        if sess.kubectl_target is None and self.settings.public_base_url:
            env["AGENTIS_URL"] = self.settings.public_base_url
        config = ClaudeRunConfig(
            cwd=sess.worktree,
            model=(adapter_opts.model if adapter_opts and adapter_opts.model else None),
            agent=(adapter_opts.agent if adapter_opts and adapter_opts.agent else None),
            effort=(adapter_opts.variant if adapter_opts and adapter_opts.variant else None),
            resume_session_id=resume_id,
            dangerously_skip_permissions=True,
            env=env,
            kubectl_target=sess.kubectl_target,
        )
        return ClaudeCodeClient(config=config)
