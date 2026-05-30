from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from collections.abc import Mapping, Sequence
from typing import Any

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
    settings: Settings,
    dispatch: Mapping[str, JsonRpcRoute],
    service_container: Any,
) -> None:
    """Run the passive WebSocket client.

    External Agentis JSON-RPC is received over the outbound WebSocket connection.
    The adapter no longer listens on an HTTP port: the agent runtime does not call
    back into the adapter (its activity is streamed directly from the CLI output).
    """
    await run_passive_websocket(settings=settings, dispatch=dispatch, service_container=service_container)


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
    asyncio.run(
        _run_websocket_transport(
            settings=settings,
            dispatch=module._DISPATCH,
            service_container=app.state,
        )
    )


def main() -> None:
    run()
