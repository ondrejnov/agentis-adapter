#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import get_settings
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError


DONE_STATUS = 5
DEFAULT_LIMIT = 1
KNOWLEDGE_LABEL_ID = "019e06c8-135d-798f-9747-64e6955708e4"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Agentis tasks that are done, assigned to a project, and have no labels."
    )
    parser.add_argument("--project", default="Agentis", help="Exact project name to export from. Default: Agentis")
    parser.add_argument("--project-id", help="Project UUID. If set, project lookup by name is skipped.")
    parser.add_argument("--endpoint", help="Agentis base URL. Defaults to AGENTIS_ENDPOINT/.env settings.")
    parser.add_argument("--token", help="Agentis API token. Defaults to AGENTIS_TOKEN/.env settings.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Page size. Default: {DEFAULT_LIMIT}")
    parser.add_argument("--from-date", help="Only process tasks updated on or after this date/datetime.")
    parser.add_argument("--json", action="store_true", default=True, help="Print raw JSON instead of a compact table.")
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


def fetch_tasks(client: AgentisJsonRpcClient, project_id: str, limit: int, from_date: str | None) -> list[dict[str, Any]]:
    filter_values: dict[str, Any] = {"project": project_id, "status": DONE_STATUS, "labels": {"empty": True}}
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


def fetch_task_detail(client: AgentisJsonRpcClient, task_id: str) -> dict[str, Any]:
    result = client.call("task.fetch", {"id": task_id})
    if not isinstance(result, dict):
        raise RuntimeError(f"task.fetch returned an unexpected response for {task_id}: {result!r}")
    return result


def create_knowledge_extract_task(client: AgentisJsonRpcClient, task_id: str) -> str:
    result = client.call("task.create_knowledge_extract_task", {"id": task_id})
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError(
            f"task.create_knowledge_extract_task returned an unexpected response for {task_id}: {result!r}"
        )
    return str(result["id"])


def related_tasks_with_labels(client: AgentisJsonRpcClient, related_tasks: list[Any]) -> list[dict[str, Any]]:
    enriched = []
    for related_task in related_tasks:
        if not isinstance(related_task, dict) or not related_task.get("id"):
            continue

        related_detail = fetch_task_detail(client, str(related_task["id"]))
        raw_related_form = related_detail.get("form")
        related_form = raw_related_form if isinstance(raw_related_form, dict) else {}
        enriched.append({**related_task, "labels": related_form.get("labels", [])})
    return enriched


def has_related_knowledge_label(related_tasks: list[dict[str, Any]]) -> bool:
    for related_task in related_tasks:
        labels = related_task.get("labels")
        if not isinstance(labels, list):
            continue
        if any(isinstance(label, dict) and label.get("id") == KNOWLEDGE_LABEL_ID for label in labels):
            return True
    return False


def print_table(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        print("No matching tasks found.")
        return

    print(f"Found {len(tasks)} matching tasks:\n")
    for task in tasks:
        number = task.get("number")
        title = str(task.get("title") or "").strip()
        task_id = task.get("id")
        prefix = f"#{number}" if number is not None else str(task_id)
        print(f"{prefix}\t{title}\t{task_id}")


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

            for task in tasks:
                task_id = str(task["id"])
                detail = fetch_task_detail(client, task_id)
                raw_form = detail.get("form")
                form = raw_form if isinstance(raw_form, dict) else {}
                raw_related_tasks = form.get("related_tasks")
                related_tasks = raw_related_tasks if isinstance(raw_related_tasks, list) else []
                enriched_related_tasks = related_tasks_with_labels(client, related_tasks)
                if has_related_knowledge_label(enriched_related_tasks):
                    continue

                knowledge_extract_task_id = create_knowledge_extract_task(client, task_id)
                print(
                    json.dumps(
                        {
                            "id": task_id,
                            "number": task.get("number"),
                            "title": task.get("title"),
                            "related_tasks": enriched_related_tasks,
                            "knowledge_extract_task_id": knowledge_extract_task_id,
                        },
                        ensure_ascii=False,
                    )
                )
    except (AgentisJsonRpcError, RuntimeError, ValueError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
