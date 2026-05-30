#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import get_settings
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError


TODO_STATUS = 2
DEFAULT_LIMIT = 500
DEFAULT_SLEEP_SECONDS = 45
KNOWLEDGE_LABEL_ID = "019e06c8-135d-798f-9747-64e6955708e4"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Agentis tasks in ToDo status with the knowledge label.")
    parser.add_argument("--project", default="Agentis", help="Exact project name to export from. Default: Agentis")
    parser.add_argument("--project-id", help="Project UUID. If set, project lookup by name is skipped.")
    parser.add_argument("--endpoint", help="Agentis base URL. Defaults to AGENTIS_ENDPOINT/.env settings.")
    parser.add_argument("--token", help="Agentis API token. Defaults to AGENTIS_TOKEN/.env settings.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Page size. Default: {DEFAULT_LIMIT}")
    parser.add_argument("--from-date", help="Only process tasks updated on or after this date/datetime.")
    parser.add_argument(
        "--sleep-seconds",
        type=int,
        default=DEFAULT_SLEEP_SECONDS,
        help=f"Delay between started runs. Default: {DEFAULT_SLEEP_SECONDS}",
    )
    return parser.parse_args()


def paged_get_list(
    client: AgentisJsonRpcClient,
    method: str,
    filter_values: dict[str, Any],
    *,
    limit: int,
    sort: dict[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    page = 1
    while True:
        result = client.call(
            method,
            {
                "qo": {
                    "filter": filter_values,
                    "sort": sort,
                    "limit": limit,
                    "page": page,
                }
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"{method} returned an unexpected response: {result!r}")

        items = result.get("items") or []
        if not isinstance(items, list):
            raise RuntimeError(f"{method} returned an unexpected items payload: {items!r}")

        yield from items

        if not result.get("more"):
            break
        page += 1


def resolve_project_id(client: AgentisJsonRpcClient, project_name: str, limit: int) -> str:
    projects = list(
        paged_get_list(
            client,
            "project.get_list",
            {"name": project_name},
            limit=limit,
            sort={"column": "name", "direction": "asc"},
        )
    )
    exact_matches = [project for project in projects if project.get("name") == project_name]

    if not exact_matches:
        raise RuntimeError(f"Project {project_name!r} was not found.")
    if len(exact_matches) > 1:
        ids = ", ".join(str(project.get("id")) for project in exact_matches)
        raise RuntimeError(f"Project name {project_name!r} is ambiguous. Use --project-id. Matching IDs: {ids}")

    project_id = exact_matches[0].get("id")
    if not project_id:
        raise RuntimeError(f"Project {project_name!r} has no id in the API response.")
    return str(project_id)


def build_updated_filter(from_date: str | None) -> dict[str, str] | None:
    if not from_date:
        return None
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        start = datetime.fromisoformat(from_date.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)

    return {
        "startDate": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "endDate": datetime.max.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z"),
    }


def fetch_tasks(
    client: AgentisJsonRpcClient, project_id: str, limit: int, from_date: str | None
) -> list[dict[str, Any]]:
    filter_values: dict[str, Any] = {"project": project_id, "status": TODO_STATUS, "labels": {"id": KNOWLEDGE_LABEL_ID}}
    updated_filter = build_updated_filter(from_date)
    if updated_filter:
        filter_values["updated"] = updated_filter

    return list(
        paged_get_list(
            client,
            "task.get_list",
            filter_values,
            limit=limit,
            sort={"column": "number", "direction": "asc"},
        )
    )


def start_task_run(client: AgentisJsonRpcClient, task_id: str) -> dict[str, Any]:
    result = client.call("task.start_run", {"id": task_id, "start_adapter": True})
    if not isinstance(result, dict):
        raise RuntimeError(f"task.start_run returned an unexpected response for {task_id}: {result!r}")
    return result


def main() -> int:
    args = parse_args()
    settings = get_settings()
    endpoint = args.endpoint or settings.agentis_endpoint
    token = args.token or settings.agentis_token

    if not endpoint:
        print("Agentis endpoint is missing. Set AGENTIS_ENDPOINT or pass --endpoint.", file=sys.stderr)
        return 2

    try:
        with AgentisJsonRpcClient(endpoint=endpoint, token=token) as client:
            project_id = args.project_id or resolve_project_id(client, args.project, args.limit)
            tasks = fetch_tasks(client, project_id, args.limit, args.from_date)
            for index, task in enumerate(tasks, start=1):
                task_id = str(task["id"])
                start_task_run(client, task_id)
                print(task.get("number"))
                if index < len(tasks) and args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)

    except (AgentisJsonRpcError, RuntimeError, ValueError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
