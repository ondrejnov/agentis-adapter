"""Adapter that runs the local `claude-p` CLI per task worktree.

Thin specialisation of :class:`CliAdapterService`: only the CLI label differs.
All the shared git/worktree/session plumbing lives in the base. Drop-in
alternativa k :class:`claude.adapter.ClaudeCodeAdapterService` — `claude-p`
interně volá tentýž Claude Code engine, jen s prokládaným transkriptem na výstupu.
"""

from __future__ import annotations

from common.cli_adapter import CliAdapterService


class ClaudePAdapterService(CliAdapterService):
    """Adapter driving the local `claude-p` CLI."""

    runtime_label = "claude-p"


__all__ = ["ClaudePAdapterService"]
