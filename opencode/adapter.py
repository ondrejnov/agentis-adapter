"""Adapter that prepares a git worktree for OpenCode.

Run lifecycle (agent execution, commit, PR) běží přes workflow runtime; adapter
poskytuje jen git worktree/snapshot plumbing z :class:`~common.git_adapter.GitAdapterService`.
"""

from __future__ import annotations

from common.git_adapter import GitAdapterService


class OpenCodeAdapterService(GitAdapterService):
    """Adapter pro worktree lokálního `opencode run`."""


__all__ = ["OpenCodeAdapterService"]
