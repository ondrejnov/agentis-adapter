"""Background runner for local `claude-p` sessions.

Sourozenec :class:`claude.session_manager.ClaudeSessionManager`: stejná
orchestrace (streaming, activity-log forwarding, dokončovací akce) z
``BaseSessionManager``, jen s `claude-p` CLI klientem (prokládaný transkript)
místo `claude`. Normalizované eventy mají identický tvar, takže transcript skládá
tentýž :class:`claude.activity_mapper.ClaudeActivityMapper`.
"""

from __future__ import annotations

from typing import Optional

from common.session_manager import BaseSessionManager, _AgentSession
from claude.activity_mapper import ClaudeActivityMapper
from claude_p.client import ClaudePClient, ClaudeRunConfig

__all__ = [
    "ClaudePSessionManager",
    "ClaudePClient",
    "ClaudeRunConfig",
]


class ClaudePSessionManager(BaseSessionManager):
    """Owns background `claude-p` runs keyed by the real Claude session_id."""

    _AGENT_LABEL = "claude-p"

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
            agent="claude-p",
            cwd=cwd,
            session_id_hint=session_id_hint,
        )

    def _build_client(self, sess: _AgentSession, resume_id: Optional[str]) -> ClaudePClient:
        adapter_opts = sess.context.adapter
        env: dict[str, str] = {"IS_SANDBOX": "1"}
        if self.settings.public_base_url:
            env["AGENTIS_URL"] = self.settings.public_base_url
        config = ClaudeRunConfig(
            command="claude-p",
            cwd=sess.worktree,
            model=(adapter_opts.model if adapter_opts and adapter_opts.model else None),
            agent=(adapter_opts.agent if adapter_opts and adapter_opts.agent else None),
            effort=(adapter_opts.effort if adapter_opts and adapter_opts.effort else None),
            resume_session_id=resume_id,
            dangerously_skip_permissions=True,
            env=env,
        )
        return ClaudePClient(config=config)
