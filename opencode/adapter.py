"""Adapter that runs OpenCode through a one-shot `opencode run`.

Na rozdíl od :class:`KubernetesAdapterService` nedeployuje OpenCode web server
ani interní REST/API — `opencode run --format json` se spustí na jedno zadání
promptu a jeho streamovaný výstup forwardujeme do Agentisu (viz
:class:`OpenCodeSessionManager`).

Sourozenec :class:`ClaudeCodeAdapterService`: oba sdílejí lifecycle ze společné
:class:`CliAdapterService` a nemají spolu nic společného navzájem. OpenCode se
od Claude liší jen labelem a tím, že běží vždy defaultně lokálně (neřeší
``claude_run_mode``).
"""

from __future__ import annotations

from common.cli_adapter import KUBERNETES_MODE, LOCAL_MODE, CliAdapterService


class OpenCodeAdapterService(CliAdapterService):
    """Adapter driving a local (or `kubectl exec`-ed) `opencode run`."""

    runtime_label = "opencode"


__all__ = ["OpenCodeAdapterService", "KUBERNETES_MODE", "LOCAL_MODE"]
