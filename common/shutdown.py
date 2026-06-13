"""Graceful shutdown: po zavření WebSocket transportu nechá doběhnout běžící práci.

Adapter drží běžící workflow runy (``WorkflowManager``) v daemon threadech —
při okamžitém ukončení procesu by se
jejich výsledky (commit, PR, agent comment, workflow outputs) ztratily.
``drain_running_work`` projde služby na ``app.state``, které umí ``wait_idle``,
a počká, až všechny nahlásí idle nebo vyprší grace perioda.
"""

from __future__ import annotations

import logging
import time
from typing import Any


logger = logging.getLogger(__name__)


def _waitable_services(service_container: Any) -> list[tuple[str, Any]]:
    state = getattr(service_container, "_state", None)
    items = state.items() if isinstance(state, dict) else vars(service_container).items()
    return [
        (name, service)
        for name, service in items
        if callable(getattr(service, "wait_idle", None)) and callable(getattr(service, "active_count", None))
    ]


def drain_running_work(service_container: Any, *, timeout: float | None = None) -> bool:
    """Blokuje, dokud služby s ``wait_idle`` nedoběhnou.

    ``timeout`` <= 0 nebo ``None`` čeká bez limitu. Vrací ``False``, pokud po
    vypršení grace periody stále něco běží (proces se pak ukončí a daemon
    thready s rozběhnutou prací zaniknou).
    """
    effective_timeout = timeout if timeout is not None and timeout > 0 else None
    deadline = time.monotonic() + effective_timeout if effective_timeout is not None else None
    completed = True
    for name, service in _waitable_services(service_container):
        active = service.active_count()
        if not active:
            continue
        logger.info("Graceful shutdown: waiting for %d running task(s) in %s", active, name)
        remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
        if service.wait_idle(remaining):
            logger.info("Graceful shutdown: %s is idle", name)
        else:
            completed = False
            logger.warning(
                "Graceful shutdown: grace period expired, %s still has %d running task(s)",
                name,
                service.active_count(),
            )
    return completed


__all__ = ["drain_running_work"]
