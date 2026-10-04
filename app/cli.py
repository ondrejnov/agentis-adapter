from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from common.config import Settings, get_settings
from common.rpc.dispatcher import JsonRpcRoute
from common.rpc.passive_websocket import run_passive_websocket
from common.status import get_status_registry
from common.workflow.devrun import RUNTIMES, SCOPES


logger = logging.getLogger(__name__)


async def _run_transports(
    *,
    settings: Settings,
    dispatch: Mapping[str, JsonRpcRoute],
    app: Any,
) -> None:
    """Run the adapter WebSocket client plus a local read-only status HTTP server.

    External Agentis JSON-RPC is received over the outbound WebSocket connection;
    the agent runtime does not call back into the adapter (its activity is
    streamed directly from the CLI output). The HTTP server serves only the
    observability endpoints (``/health``, ``/status``, logs).
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
    parser = argparse.ArgumentParser(
        prog="agentis-adapter",
        description="Bez příkazu spustí adapter (WebSocket spojení s Agentisem).",
    )
    parser.add_argument("--id", help="Agentis adapter id. Defaults to AGENTIS_ADAPTER_ID.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    workflow = commands.add_parser("workflow", help="Testování workflow bez Agentisu.")
    workflow_commands = workflow.add_subparsers(dest="workflow_command", metavar="ACTION", required=True)

    run_cmd = workflow_commands.add_parser(
        "run",
        help="Spustí workflow lokálně a vypíše kroky, komentáře a outputs.",
        description="Spustí workflow stejnou cestou jako Agentis `start`; výsledky místo do Agentisu vypíše.",
    )
    run_cmd.add_argument("workflow", help="Jméno workflow (ai-news, default, project) nebo cesta k YAML.")
    run_cmd.add_argument("prompt", nargs="?", default=None, help="Text zadání ($AGENTIS_PROMPT_FILE).")
    run_cmd.add_argument("-f", "--prompt-file", help="Zadání ze souboru; '-' = stdin.")
    run_cmd.add_argument("-C", "--project", type=Path, help="Adresář projektu (default: nejbližší s .agentis/).")
    run_cmd.add_argument(
        "-r",
        "--runtime",
        choices=RUNTIMES,
        default="local",
        help="local = bash na hostu (default), docker, workflow = executor z YAML / WORKFLOW_EXECUTOR.",
    )
    run_cmd.add_argument("--scope", choices=SCOPES, help="Scope runu (default project; pro default.yaml task).")
    run_cmd.add_argument("-m", "--model", help="AGENTIS_MODEL (default: nenastaveno, platí model z workflow).")
    run_cmd.add_argument("-e", "--effort", help="AGENTIS_EFFORT (default: nenastaveno).")
    run_cmd.add_argument("--title", help="Název mock tasku (TASK_TITLE).")
    run_cmd.add_argument("--full", action="store_true", help="Vypíše komentáře celé, bez zkrácení.")
    run_cmd.add_argument(
        "--agentis", action="store_true", help="Posílat eventy a outputs do skutečného Agentisu (AGENTIS_ENDPOINT)."
    )

    validate_cmd = workflow_commands.add_parser(
        "validate", help="Zkontroluje workflow YAML (bez args všechna workflow projektu)."
    )
    validate_cmd.add_argument("workflows", nargs="*", help="Jména workflow nebo cesty k YAML.")
    validate_cmd.add_argument("-C", "--project", type=Path, help="Adresář projektu.")

    list_cmd = workflow_commands.add_parser("list", help="Vypíše workflow projektu a zabalená workflow adapteru.")
    list_cmd.add_argument("-C", "--project", type=Path, help="Adresář projektu.")
    return parser


def _run_workflow_command(args: argparse.Namespace) -> int:
    from common.workflow import devrun

    cwd = Path.cwd()
    try:
        if args.workflow_command == "run":
            return devrun.run_workflow(
                args.workflow,
                prompt=devrun.read_prompt(args.prompt, args.prompt_file),
                project=args.project,
                cwd=cwd,
                runtime=args.runtime,
                scope=args.scope,
                model=args.model,
                effort=args.effort,
                title=args.title,
                agentis_callbacks=args.agentis,
                full=args.full,
            )
        if args.workflow_command == "validate":
            return devrun.validate_workflows(args.workflows, args.project, cwd)
        project = args.project.resolve() if args.project else devrun.find_project(cwd)
        return devrun.list_workflows(project, get_settings())
    except devrun.WorkflowCliError as exc:
        sys.stderr.write(f"agentis-adapter: {exc}\n")
        return 2


def run(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)

    if args.command == "workflow":
        raise SystemExit(_run_workflow_command(args))

    if args.id is not None:
        os.environ["AGENTIS_ADAPTER_ID"] = args.id
    get_settings.cache_clear()

    settings = get_settings()
    # Konkrétní CLI agent se vybírá až v workflow kroku; serving adapter je jeden
    # generický (worktree/snapshot plumbing), proto žádný `--adapter` výběr.
    from app import adapter_api

    app = adapter_api.create_app()
    get_status_registry().set_meta(adapter="agentis", adapter_id=settings.agentis_adapter_id)

    asyncio.run(
        _run_transports(
            settings=settings,
            dispatch=adapter_api._DISPATCH,
            app=app,
        )
    )


def main() -> None:
    run()
