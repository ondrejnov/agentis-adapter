#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import get_settings
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError


TODO_STATUS = 2
DEFAULT_LIMIT = 500
WRITE_LABEL_NAME = "Documentor"
WRITE_TASK_DESCRIPTION = "Projdi přiložené znalosti a doplň podle nich dokumentaci."
CODE_LABEL_NAME = "Code"
DOCUMENTATION_IN_PROCESS_KEY = "documentation_in_process"
WRITE_TASK_MODEL_ID = "019e3a29-c286-72db-9499-ff485e23b65a"
WRITE_TASK_EFFORT = "high"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create Agentis write tasks for tasks with non-empty knowledge.")
    parser.add_argument("--project", default="Agentis", help="Exact project name to export from. Default: Agentis")
    parser.add_argument("--project-id", help="Project UUID. If set, project lookup by name is skipped.")
    parser.add_argument("--endpoint", help="Agentis base URL. Defaults to AGENTIS_ENDPOINT/.env settings.")
    parser.add_argument("--token", help="Agentis API token. Defaults to AGENTIS_TOKEN/.env settings.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Page size. Default: {DEFAULT_LIMIT}")
    parser.add_argument("--dry-run", action="store_true", help="Print matching tasks without creating follow-up tasks.")
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


def resolve_label_id(client: AgentisJsonRpcClient, label_name: str, limit: int) -> str:
    labels = list(
        paged_get_list(
            client,
            "label.get_list",
            {"name": label_name},
            limit=limit,
            sort={"column": "name", "direction": "asc"},
        )
    )
    exact_matches = [label for label in labels if label.get("name") == label_name]

    if not exact_matches:
        raise RuntimeError(f"Label {label_name!r} was not found.")
    if len(exact_matches) > 1:
        ids = ", ".join(str(label.get("id")) for label in exact_matches)
        raise RuntimeError(f"Label name {label_name!r} is ambiguous. Matching IDs: {ids}")

    label_id = exact_matches[0].get("id")
    if not label_id:
        raise RuntimeError(f"Label {label_name!r} has no id in the API response.")
    return str(label_id)


def fetch_tasks(client: AgentisJsonRpcClient, project_id: str, code_label_id: str, limit: int) -> list[dict[str, Any]]:
    return list(
        paged_get_list(
            client,
            "task.get_list",
            {"project": project_id, "labels": code_label_id},
            limit=limit,
            sort={"column": "number", "direction": "asc"},
        )
    )


def fetch_task_detail(client: AgentisJsonRpcClient, task_id: str) -> dict[str, Any]:
    result = client.call("task.fetch", {"id": task_id})
    if not isinstance(result, dict):
        raise RuntimeError(f"task.fetch returned an unexpected response for {task_id}: {result!r}")
    return result


def has_non_empty_knowledge(form: dict[str, Any]) -> bool:
    knowledge = form.get("knowledge")
    if knowledge is None:
        return False
    if isinstance(knowledge, (dict, list, str)):
        return bool(knowledge)
    return True


def get_custom_data(form: dict[str, Any]) -> dict[str, Any]:
    custom_data = form.get("custom_data")
    return dict(custom_data) if isinstance(custom_data, dict) else {}


def has_documentation_process_state(form: dict[str, Any]) -> bool:
    return DOCUMENTATION_IN_PROCESS_KEY in get_custom_data(form)


def selector_id(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("id")
    return value


def build_task_update_data(task_id: str, form: dict[str, Any], custom_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": form.get("title"),
        "meta_title": form.get("meta_title"),
        "meta_description": form.get("meta_description"),
        "project": selector_id(form.get("project")),
        "agent": selector_id(form.get("agent")),
        "status": form.get("status"),
        "sprint": form.get("sprint"),
        "priority": form.get("priority"),
        "description": form.get("description"),
        "metadata": form.get("metadata"),
        "headers": form.get("headers"),
        "custom_data": custom_data,
        "knowledge": form.get("knowledge"),
        "expected_artifacts": form.get("expected_artifacts"),
        "attachments": form.get("attachments"),
        "adapter_options": form.get("adapter_options"),
        "model": selector_id(form.get("model")),
        "effort": form.get("effort"),
        "adapter": selector_id(form.get("adapter")),
        "environment": selector_id(form.get("environment")),
        "notice": form.get("notice"),
        "scheduled_at": form.get("scheduled_at"),
        "worktree": form.get("worktree"),
        "working_dir": form.get("working_dir"),
    }


def mark_documentation_in_process(client: AgentisJsonRpcClient, task_id: str, form: dict[str, Any]) -> dict[str, Any]:
    custom_data = get_custom_data(form)
    custom_data[DOCUMENTATION_IN_PROCESS_KEY] = True
    result = client.call("task.save", {"data": build_task_update_data(task_id, form, custom_data)})
    if not isinstance(result, dict):
        raise RuntimeError(
            f"task.save returned an unexpected response while updating custom_data for {task_id}: {result!r}"
        )
    return custom_data


def convert_to_lexical(text: str) -> dict[str, Any]:
    return {
        "root": {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [{"type": "text", "text": line}],
                }
                for line in text.splitlines()
                if line.strip()
            ],
        }
    }


def serialize_knowledge_metadata(knowledge: Any) -> str:
    return f"<knowledge>{json.dumps(knowledge, ensure_ascii=False, indent=2)}</knowledge>"


def build_write_title(source_task: dict[str, Any]) -> str:
    source_title = str(source_task.get("title") or "").strip()
    if source_title:
        return f"Doc: {source_title}"[:255]

    if source_task.get("number") is not None:
        return f"Doc: Task #{source_task['number']}"

    return f"Doc: Task {source_task['id']}"


def create_write_task(
    client: AgentisJsonRpcClient,
    source_task: dict[str, Any],
    source_form: dict[str, Any],
    label_ids: list[str],
) -> str:
    source_task_id = str(source_task["id"])
    result = client.call(
        "task.save",
        {
            "data": {
                "title": build_write_title(source_task),
                "project": source_task.get("project"),
                "status": TODO_STATUS,
                "description": convert_to_lexical(WRITE_TASK_DESCRIPTION),
                "metadata": serialize_knowledge_metadata(source_form.get("knowledge")),
                "labels": label_ids,
                "related_tasks": [source_task_id],
                "worktree": False,
                "agent": "019e06de-7b04-779f-a5c4-e7249c96fe91",
                "model": WRITE_TASK_MODEL_ID,
                "effort": WRITE_TASK_EFFORT,
                # "adapter_options": [{"key": "branch", "value": "docs"}],
            }
        },
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"task.save returned an unexpected response for {source_task_id}: {result!r}")

    raw_form = result.get("form")
    form = raw_form if isinstance(raw_form, dict) else result
    new_task_id = form.get("id")
    if not new_task_id:
        raise RuntimeError(f"task.save did not return a new task id for {source_task_id}: {result!r}")
    return str(new_task_id)


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
            write_label_id = resolve_label_id(client, WRITE_LABEL_NAME, args.limit)
            code_label_id = resolve_label_id(client, CODE_LABEL_NAME, args.limit)

            for task in fetch_tasks(client, project_id, code_label_id, args.limit):
                task_id = str(task["id"])
                detail = fetch_task_detail(client, task_id)
                raw_form = detail.get("form")
                form = raw_form if isinstance(raw_form, dict) else {}
                if has_documentation_process_state(form):
                    continue
                if not has_non_empty_knowledge(form):
                    continue

                write_task_id = None
                run_result = None
                custom_data = get_custom_data(form)
                if not args.dry_run:
                    write_task_id = create_write_task(client, task, form, [write_label_id])
                    custom_data = mark_documentation_in_process(client, task_id, form)
                    run_result = start_task_run(client, write_task_id)

                if not args.dry_run:
                    time.sleep(300)

                print(
                    json.dumps(
                        {
                            "id": task_id,
                            "number": task.get("number"),
                            "title": task.get("title"),
                            "knowledge": form.get("knowledge"),
                            "custom_data": custom_data,
                            "write_task_id": write_task_id,
                            "run_result": run_result,
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
