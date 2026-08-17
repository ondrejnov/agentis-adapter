"""Agentis integration wrapper around the external ``agentiscode`` package."""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys
from typing import Any, Optional, Sequence

from agentiscode import AgentConfig, normalize_adapter
from agentiscode.cli import OutputRecorder, build_parser, read_prompt, run_agent
from common.agentis_telemetry import AgentisTelemetry

_run = run_agent


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true/false")


def _parser() -> argparse.ArgumentParser:
    parser = build_parser(prog="agentiscode-agentis")
    parser.description = "Agentis telemetry integration for the AgentisCode CLI."
    parser.add_argument("--task-id", metavar="TASK_ID", help="Agentis task id.")
    parser.add_argument(
        "--project-id",
        metavar="PROJECT_ID",
        default=os.environ.get("AGENTIS_PROJECT_ID"),
        help="Agentis project id added to a new prompt (default: $AGENTIS_PROJECT_ID).",
    )
    parser.add_argument("--run-id", metavar="RUN_ID", help="Existing Agentis run id.")
    parser.add_argument("--task-status", type=int, metavar="STATUS_ID")
    parser.add_argument("--last-message-to-comment", action="store_true")
    parser.add_argument("--primary-session", type=_parse_bool, default=True, metavar="BOOL")
    parser.add_argument("--agentis-api", default=os.environ.get("AGENTIS_ENDPOINT"), metavar="URL")
    parser.add_argument(
        "--agentis-token",
        default=os.environ.get("AGENTIS_API_TOKEN") or os.environ.get("AGENTIS_TOKEN"),
        metavar="TOKEN",
    )
    parser.add_argument(
        "--agentis-service-token",
        default=os.environ.get("AGENTIS_SERVICE_TOKEN"),
        metavar="TOKEN",
    )
    return parser


def _append_context_ids(prompt: str, task_id: Optional[str], project_id: Optional[str]) -> str:
    tags = []
    if task_id:
        tags.append(f"<agentis_task_id>{task_id}</agentis_task_id>")
    if project_id:
        tags.append(f"<agentis_project_id>{project_id}</agentis_project_id>")
    if not tags:
        return prompt
    return f"{prompt.rstrip()}\n\n" + "\n".join(tags)


def _command_display(argv: Sequence[str], *, executable: str = "agentiscode-agentis") -> str:
    sensitive_options = {"--agentis-token", "--agentis-service-token"}
    display_args = [executable]
    redact_next = False
    for arg in argv:
        if redact_next:
            display_args.append("REDACTED")
            redact_next = False
            continue
        option, separator, _value = arg.partition("=")
        if option in sensitive_options:
            display_args.append(f"{option}=REDACTED" if separator else option)
            redact_next = not separator
            continue
        display_args.append(arg)
    return shlex.join(display_args)


def run(argv: Optional[Sequence[str]] = None) -> int:
    cli_args = list(sys.argv[1:] if argv is None else argv)
    executable = sys.argv[0] if argv is None else "agentiscode-agentis"
    sys.stderr.write(f"[agentiscode-agentis] command: {_command_display(cli_args, executable=executable)}\n")
    sys.stderr.flush()

    parser = _parser()
    args = parser.parse_args(cli_args)
    try:
        adapter = normalize_adapter(args.adapter)
    except ValueError as exc:
        parser.error(str(exc))

    prompt = read_prompt(args.prompt)
    if not prompt:
        parser.error("Missing prompt (provide it as an argument or on stdin).")
    if args.run_id and not args.task_id:
        parser.error("--run-id requires --task-id.")
    if (args.task_id or args.run_id) and not args.agentis_api:
        parser.error("--task-id/--run-id requires --agentis-api or $AGENTIS_ENDPOINT.")
    if not args.resume:
        prompt = _append_context_ids(prompt, args.task_id, args.project_id)

    cwd = args.cwd or os.getcwd()
    config = AgentConfig(
        adapter=adapter,
        model=args.model,
        effort=args.effort,
        agent=args.agent,
        cwd=cwd,
        resume_session_id=args.resume,
        timeout_sec=args.timeout,
    )
    handlers: list[Any] = []
    telemetry: Optional[AgentisTelemetry] = None
    if args.task_id or args.run_id:
        def _telemetry_error(message: str) -> None:
            sys.stderr.write(f"[agentiscode-agentis] {message}\n")

        telemetry = AgentisTelemetry(
            task_id=args.task_id,
            prompt=prompt,
            adapter=adapter,
            mode=args.agent or "build",
            cwd=cwd,
            run_id=args.run_id,
            task_status=args.task_status,
            last_message_to_comment=args.last_message_to_comment,
            primary_session=args.primary_session,
            endpoint=args.agentis_api,
            token=args.agentis_token,
            service_token=args.agentis_service_token,
            on_error=_telemetry_error,
        )
        handlers.append(telemetry)
    if args.final_output or args.session_output:
        handlers.append(OutputRecorder(args.final_output, args.session_output))

    try:
        return asyncio.run(_run(config, prompt, args.json, handlers))
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130
    finally:
        if telemetry is not None:
            telemetry.close()


def main() -> None:
    raise SystemExit(run())


__all__ = ["run", "main"]
