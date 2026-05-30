from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from collections.abc import Mapping, Sequence
from typing import Any

import uvicorn

from common.config import Settings, get_settings
from common.rpc.dispatcher import JsonRpcRoute
from common.rpc.passive_websocket import run_passive_websocket


_ADAPTER_MODULES = {
    "opencode": "opencode.api",
    "claude": "claude.api",
    "claudecode": "claude.api",
}


async def _run_websocket_transport(
    *,
    app: Any,
    settings: Settings,
    dispatch: Mapping[str, JsonRpcRoute],
    service_container: Any,
) -> None:
    """Run the passive WebSocket client alongside a local HTTP listener.

    External Agentis JSON-RPC is received over the outbound WebSocket connection.
    The HTTP listener on ``host:port`` only serves internal callbacks (``/api-internal``)
    from the agent runtime and the ``/health`` endpoint.
    """
    config = uvicorn.Config(app, host=settings.host, port=settings.port, reload=False)
    server = uvicorn.Server(config)

    http_task = asyncio.create_task(server.serve())
    websocket_task = asyncio.create_task(
        run_passive_websocket(settings=settings, dispatch=dispatch, service_container=service_container)
    )

    done, pending = await asyncio.wait({http_task, websocket_task}, return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True
    if websocket_task in pending:
        websocket_task.cancel()

    pending_results = await asyncio.gather(*pending, return_exceptions=True)
    for result in pending_results:
        if isinstance(result, asyncio.CancelledError):
            continue
        if isinstance(result, BaseException):
            raise result

    for task in done:
        task.result()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentis-adapter")
    parser.add_argument(
        "--adapter",
        choices=sorted(_ADAPTER_MODULES),
        required=True,
        help="Adapter runtime to serve.",
    )
    parser.add_argument("--host", help="Internal listener bind host. Defaults to ADAPTER_HOST or 0.0.0.0.")
    parser.add_argument("--port", type=int, help="Internal listener bind port. Defaults to ADAPTER_PORT or 8001.")
    parser.add_argument("--id", help="Agentis adapter id. Defaults to AGENTIS_ADAPTER_ID.")
    return parser


def run(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)

    if args.host is not None:
        os.environ["ADAPTER_HOST"] = args.host
    if args.port is not None:
        os.environ["ADAPTER_PORT"] = str(args.port)
    if args.id is not None:
        os.environ["AGENTIS_ADAPTER_ID"] = args.id
    get_settings.cache_clear()

    settings = get_settings()
    module = importlib.import_module(_ADAPTER_MODULES[args.adapter])
    app = module.create_app()
    asyncio.run(
        _run_websocket_transport(
            app=app,
            settings=settings,
            dispatch=module._DISPATCH,
            service_container=app.state,
        )
    )


def main() -> None:
    run()
