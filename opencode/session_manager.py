"""Background runner for local `opencode run` sessions.

OpenCode se zde spouští jako jednorázové `opencode run <prompt> --format
json`) — bez web REST API a bez interního API pluginu. Streamovaný výstup
parsujeme a forwardujeme do Agentisu přímo, stejně jako u Claude Code adaptéru.

Veškerá orchestrace (streaming, store_activity_log, dokončovací akce) se dědí
z ``ClaudeSessionManager``; přepisujeme jen agentně specifické hooky.
"""

from __future__ import annotations

from typing import Optional

from claude.session_manager import ClaudeSessionManager, _ClaudeSession
from opencode.runner import OpenCodeRunner, OpenCodeRunConfig
from opencode.activity_mapper import OpenCodeActivityMapper


class OpenCodeSessionManager(ClaudeSessionManager):
    """Owns background `opencode run` runs keyed by the OpenCode session_id."""

    _AGENT_LABEL = "opencode"
    _REMOTE_PKILL_PATTERN = "opencode run"

    def _make_mapper(
        self,
        *,
        prompt: str,
        mode: str,
        cwd: str,
        session_id_hint: Optional[str] = None,
    ) -> OpenCodeActivityMapper:
        return OpenCodeActivityMapper(
            prompt=prompt,
            mode=mode,
            agent=mode,
            cwd=cwd,
            session_id_hint=session_id_hint,
        )

    def _build_client(self, sess: _ClaudeSession, resume_id: Optional[str]) -> OpenCodeRunner:
        adapter_opts = sess.context.adapter
        # Záměrně NEnastavujeme AGENTIS_URL — opencode plugin tak neprovádí žádná
        # volání interního API; aktivitu streamujeme a forwardujeme my sami.
        env: dict[str, str] = {"IS_SANDBOX": "1"}
        config = OpenCodeRunConfig(
            cwd=sess.worktree,
            model=(adapter_opts.model if adapter_opts and adapter_opts.model else None),
            agent=(adapter_opts.agent if adapter_opts and adapter_opts.agent else None),
            variant=(adapter_opts.variant if adapter_opts and adapter_opts.variant else None),
            resume_session_id=resume_id,
            dangerously_skip_permissions=True,
            env=env,
            kubectl_target=sess.kubectl_target,
        )
        return OpenCodeRunner(config=config)
