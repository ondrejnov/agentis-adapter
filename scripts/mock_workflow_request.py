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

from common.config import Settings, get_settings  # noqa: E402
from common.git_adapter import GitAdapterService  # noqa: E402
from common.rpc.jsonrpc import AgentJsonRpcService  # noqa: E402
from common.workflow.manager import WorkflowManager  # noqa: E402

DEFAULT_MODEL = "openai/gpt-5.4-mini"
DEFAULT_EFFORT = "low"


def build_start_payload(
    *,
    working_dir: Path,
    prompt: str,
    workflow: str | None,
    scope: str = "project",
    runtime: str = "local",
    model: str | None = DEFAULT_MODEL,
    effort: str | None = DEFAULT_EFFORT,
    agent: str = "build",
    title: str = "Mock workflow request",
    description: str = "",
    task_id: str | None = None,
    run_id: str | None = None,
    task_number: int = 999999,
    session_id: str | None = None,
    project_slug: str | None = None,
    project_title: str | None = None,
    project_github_repo: str | None = None,
    base_branch: str = "master",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """JSON-RPC `start` ve tvaru, jaký posílá Agentis."""

    working_dir = working_dir.resolve()
    slug = project_slug or working_dir.name
    adapter: dict[str, Any] = {"scope": scope, "runtime": runtime, "agent": agent}
    # Bez modelu/effortu krok nedostane AGENTIS_MODEL/AGENTIS_EFFORT a použije default z workflow.
    if model:
        adapter["model"] = model
    if effort:
        adapter["effort"] = effort
    if workflow:
        adapter["workflow"] = workflow
    context: dict[str, Any] = {
        "run_id": run_id or f"mock-run-{uuid4().hex}",
        "task_id": task_id or f"mock-task-{uuid4().hex}",
        "session_id": session_id,
        "title": title,
        "description": description,
        "user_prompt": prompt,
        "task_number": task_number,
        "headers": headers or None,
        "project_slug": slug,
        "project_title": project_title or slug,
        "project_github_repo": project_github_repo,
        "base_branch": base_branch,
        "working_dir": str(working_dir),
        "adapter": adapter,
    }
    return {"jsonrpc": "2.0", "id": context["run_id"], "method": "start", "params": {"context": context}}


def create_service(settings: Settings, *, agentis_callbacks: bool) -> AgentJsonRpcService:
    """`AgentJsonRpcService` jako v produkci; bez `--agentis-callbacks` jdou Agentis volání jen na stdout."""

    if not agentis_callbacks:
        settings = replace(settings, agentis_endpoint=None)
    manager = WorkflowManager(settings, agentis_sink=None if agentis_callbacks else _print_agentis_call)
    return AgentJsonRpcService(
        settings=settings,
        adapter_factory=lambda context: GitAdapterService(context=context, settings=settings),
        workflow_manager=manager,
    )


def _print_agentis_call(method: str, params: dict[str, Any]) -> None:
    print(f"[agentis] {method}: {json.dumps(params, ensure_ascii=False, default=str)[:500]}", file=sys.stderr)


async def dispatch_start(payload: dict[str, Any], service: AgentJsonRpcService) -> tuple[dict[str, Any], int]:
    from app.adapter_api import _DISPATCH
    from common.rpc.dispatcher import dispatch_jsonrpc_payload

    container = SimpleNamespace(agent_jsonrpc_service=service)
    result = await dispatch_jsonrpc_payload(payload, _DISPATCH, container)
    return result.body, result.http_status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build or dispatch a local mock Agentis JSON-RPC start request for workflow testing. "
            "For workflow-only testing without the adapter prefer `agentis-workflow run <name> ...`."
        )
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
    headers = {
        key: value
        for key, value in {
            "slack_channel": args.slack_channel,
            "slack_message_ts": args.slack_message_ts,
            "slack_thread_ts": args.slack_thread_ts,
        }.items()
        if value
    }
    return build_start_payload(
        working_dir=Path(args.working_dir),
        prompt=args.prompt,
        workflow=args.workflow,
        scope=args.scope,
        runtime=args.runtime,
        model=args.model,
        effort=args.effort,
        agent=args.agent,
        title=args.title,
        description=args.description,
        task_id=args.task_id,
        run_id=args.run_id,
        task_number=args.task_number,
        session_id=args.session_id,
        project_slug=args.project_slug,
        project_title=args.project_title,
        project_github_repo=args.project_github_repo,
        base_branch=args.base_branch,
        headers=headers,
    )


async def dispatch_payload(
    payload: dict[str, Any], *, agentis_callbacks: bool
) -> tuple[dict[str, Any], int, AgentJsonRpcService]:
    service = create_service(get_settings(), agentis_callbacks=agentis_callbacks)
    body, http_status = await dispatch_start(payload, service)
    return body, http_status, service


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
