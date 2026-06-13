"""Adapter that prepares a git worktree for the local `claude` CLI.

Run lifecycle (agent execution, commit, PR) běží přes workflow runtime; adapter
poskytuje jen git worktree/snapshot plumbing z :class:`~common.git_adapter.GitAdapterService`.
"""

from __future__ import annotations

from common.git_adapter import GitAdapterService


class ClaudeCodeAdapterService(GitAdapterService):
    """Adapter pro worktree lokálního `claude` CLI."""


__all__ = ["ClaudeCodeAdapterService"]
