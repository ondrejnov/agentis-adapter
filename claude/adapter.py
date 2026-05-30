"""Adapter that runs the local `claude` CLI per task worktree.

Thin specialisation of :class:`CliAdapterService`: only the run-mode default
(which honours ``settings.claude_run_mode``) and the CLI label differ. All the
shared git/worktree/session plumbing lives in the base.
"""

from __future__ import annotations

from common.cli_adapter import KUBERNETES_MODE, LOCAL_MODE, CliAdapterService


class ClaudeCodeAdapterService(CliAdapterService):
    """Adapter driving the local (or `kubectl exec`-ed) `claude` CLI."""

    runtime_label = "claude"

    def _default_run_mode(self) -> str | None:
        return self.settings.claude_run_mode or LOCAL_MODE


__all__ = ["ClaudeCodeAdapterService", "KUBERNETES_MODE", "LOCAL_MODE"]
