from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any

from common.config import Settings, get_settings
from common.rpc.dispatcher import JsonRpcRoute
from common.rpc.passive_websocket import run_passive_websocket
from common.status import get_status_registry


logger = logging.getLogger(__name__)

_ADAPTER_MODULES = {
    "agentiscode": "agentiscode.api",
    "opencode": "opencode.api",
    "claude": "claude.api",
    "claudecode": "claude.api",
    "claude-p": "claude_p.api",
}


async def _run_transports(
    *,
    settings: Settings,
    dispatch: Mapping[str, JsonRpcRoute],
    app: Any,
) -> None:
    """Run the passive WebSocket client plus a local read-only status HTTP server.

    External Agentis JSON-RPC is received over the outbound WebSocket connection;
    the agent runtime does not call back into the adapter (its activity is
    streamed directly from the CLI output). The HTTP server serves only the
    observability endpoints (``/health``, ``/status``, logy) pro ``agentis-top``.
    """
    import uvicorn

    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning")
    server = uvicorn.Server(config)
    # Signály (SIGTERM/SIGINT) vlastní WebSocket transport — graceful shutdown
    # adapteru; uvicorn nesmí jejich handlery přepsat.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def _serve_status_api() -> None:
        # Status server je jen observabilita — jeho selhání (typicky obsazený
        # port) nesmí shodit adapter; uvicorn při bind chybě volá sys.exit(1).
        try:
            await server.serve()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            logger.warning("Status HTTP server failed (host=%s port=%s): %s", settings.host, settings.port, exc)

    server_task = asyncio.create_task(_serve_status_api())
    try:
        await run_passive_websocket(settings=settings, dispatch=dispatch, service_container=app.state)
    finally:
        server.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentis-adapter")
    parser.add_argument(
        "--adapter",
        choices=sorted(_ADAPTER_MODULES),
        required=True,
        help="Adapter runtime to serve.",
    )
    parser.add_argument("--id", help="Agentis adapter id. Defaults to AGENTIS_ADAPTER_ID.")
    return parser


def run(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)

    if args.id is not None:
        os.environ["AGENTIS_ADAPTER_ID"] = args.id
    get_settings.cache_clear()

    settings = get_settings()
    module = importlib.import_module(_ADAPTER_MODULES[args.adapter])
    app = module.create_app()
    get_status_registry().set_meta(adapter=args.adapter, adapter_id=settings.agentis_adapter_id)

    # Ingestion adapters (e.g. Slack) drive their own foreground loop instead of
    # the passive WebSocket transport: they push tasks into Agentis rather than
    # receiving agent runtime JSON-RPC.
    run_adapter = getattr(module, "run_adapter", None)
    if run_adapter is not None:
        run_adapter(settings=settings, service_container=app.state)
        return

    asyncio.run(
        _run_transports(
            settings=settings,
            dispatch=module._DISPATCH,
            app=app,
        )
    )


def main() -> None:
    run()
