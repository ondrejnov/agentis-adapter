#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapter_api import _DISPATCH  # noqa: E402
from common.config import get_settings  # noqa: E402
from common.git_adapter import GitAdapterService  # noqa: E402
from common.rpc.dispatcher import dispatch_jsonrpc_payload  # noqa: E402
from common.rpc.jsonrpc import AgentJsonRpcService  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build or dispatch a local mock Agentis JSON-RPC start request for workflow testing."
    )
    parser.add_argument("prompt", nargs="?", default="Mock Slack workflow request.", help="Prompt sent to the agent.")
    parser.add_argument("--print-only", action="store_true", help="Only print the JSON-RPC payload; do not run it.")
    parser.add_argument("--wait", action="store_true", help="Wait until the background workflow finishes.")
    parser.add_argument("--wait-timeout", type=float, default=None, help="Maximum seconds to wait with --wait.")
    parser.add_argument("--agentis-callbacks", action="store_true", help="Allow callbacks to AGENTIS_ENDPOINT.")
    parser.add_argument("--workflow", default="test", help="Named workflow from .agentis/workflows/<name>.yaml.")
    parser.add_argument("--scope", choices=("project", "task", "worktree"), default="project", help="Adapter scope.")
    parser.add_argument(
        "--runtime",
        choices=("docker", "local", "workflow"),
        default="local",
        help="Adapter runtime. 'docker' or 'local' forces the corresponding workflow executor.",
    )
    parser.add_argument("--working-dir", default=str(Path.cwd()), help="Project directory used as context.working_dir.")
    parser.add_argument("--project-slug", default=None, help="Project slug. Defaults to working directory name.")
    parser.add_argument("--project-title", default=None, help="Project title. Defaults to project slug.")
    parser.add_argument("--project-github-repo", default=None, help="Optional GitHub repo, e.g. owner/repo.")
    parser.add_argument("--base-branch", default="master", help="Base branch in the mocked context.")
    parser.add_argument("--title", default="Mock workflow request", help="Mock task title.")
    parser.add_argument("--description", default="", help="Mock task description.")
    parser.add_argument("--task-id", default=None, help="Mock task id. Defaults to mock-<uuid>.")
    parser.add_argument("--run-id", default=None, help="Mock run id. Defaults to mock-run-<uuid>.")
    parser.add_argument("--task-number", type=int, default=999999, help="Mock task number.")
    parser.add_argument("--session-id", default=None, help="Optional session id for resume-style runs.")
    parser.add_argument("--agent", default="build", help="Agent name in context.adapter.agent.")
    parser.add_argument("--model", default="openai/gpt-5.4-mini", help="Model in context.adapter.model.")
    parser.add_argument("--effort", default="low", help="Effort in context.adapter.effort.")
    parser.add_argument(
        "--slack-channel", default=None, help="Optional Slack channel header; enables Slack post steps."
    )
    parser.add_argument("--slack-message-ts", default=None, help="Optional Slack message ts header for chat.update.")
    parser.add_argument("--slack-thread-ts", default=None, help="Optional Slack thread ts header.")
    return parser.parse_args(argv)


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    working_dir = Path(args.working_dir).resolve()
    project_slug = args.project_slug or working_dir.name
    headers = {
        key: value
        for key, value in {
            "slack_channel": args.slack_channel,
            "slack_message_ts": args.slack_message_ts,
            "slack_thread_ts": args.slack_thread_ts,
        }.items()
        if value
    }
    adapter = {
        "scope": args.scope,
        "runtime": args.runtime,
        "agent": args.agent,
        "model": args.model,
        "effort": args.effort,
    }
    if args.workflow:
        adapter["workflow"] = args.workflow

    context: dict[str, Any] = {
        "run_id": args.run_id or f"mock-run-{uuid4().hex}",
        "task_id": args.task_id or f"mock-task-{uuid4().hex}",
        "session_id": args.session_id,
        "title": args.title,
        "description": args.description,
        "user_prompt": args.prompt,
        "task_number": args.task_number,
        "headers": headers or None,
        "project_slug": project_slug,
        "project_title": args.project_title or project_slug,
        "project_github_repo": args.project_github_repo,
        "base_branch": args.base_branch,
        "working_dir": str(working_dir),
        "adapter": adapter,
    }
    return {"jsonrpc": "2.0", "id": context["run_id"], "method": "start", "params": {"context": context}}


async def dispatch_payload(
    payload: dict[str, Any], *, agentis_callbacks: bool
) -> tuple[dict[str, Any], int, AgentJsonRpcService]:
    settings = get_settings()
    if not agentis_callbacks:
        settings = replace(settings, agentis_endpoint=None)
    service = AgentJsonRpcService(
        settings=settings,
        adapter_factory=lambda context: GitAdapterService(context=context, settings=settings),
    )
    container = SimpleNamespace(agent_jsonrpc_service=service)
    result = await dispatch_jsonrpc_payload(payload, _DISPATCH, container)
    return result.body, result.http_status, service


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(args)

    if args.print_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    body, http_status, service = asyncio.run(dispatch_payload(payload, agentis_callbacks=args.agentis_callbacks))
    print(json.dumps(body, ensure_ascii=False, indent=2))
    if http_status >= 400:
        return 1

    if args.wait:
        idle = service.wait_idle(args.wait_timeout)
        if not idle:
            print("Workflow is still running after wait timeout.", file=sys.stderr)
            return 124
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
