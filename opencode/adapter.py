"""Adapter that runs OpenCode through a one-shot `opencode run`.

OpenCode zde nedeployuje žádný web server ani interní REST/API — `opencode run
--format json` se spustí na jedno zadání promptu a jeho streamovaný výstup
forwardujeme do Agentisu (viz :class:`OpenCodeSessionManager`).

Sourozenec :class:`ClaudeCodeAdapterService`: oba sdílejí lifecycle ze společné
:class:`CliAdapterService` a liší se jen labelem.
"""

from __future__ import annotations

from common.cli_adapter import CliAdapterService


class OpenCodeAdapterService(CliAdapterService):
    """Adapter driving a local `opencode run`."""

    runtime_label = "opencode"


__all__ = ["OpenCodeAdapterService"]
