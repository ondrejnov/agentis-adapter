"""Adapter that runs the local `claude` CLI per task worktree.

Thin specialisation of :class:`CliAdapterService`: only the CLI label differs.
All the shared git/worktree/session plumbing lives in the base.
"""

from __future__ import annotations

from common.cli_adapter import CliAdapterService


class ClaudeCodeAdapterService(CliAdapterService):
    """Adapter driving the local `claude` CLI."""

    runtime_label = "claude"


__all__ = ["ClaudeCodeAdapterService"]
