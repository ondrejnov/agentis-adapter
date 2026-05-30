#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import get_settings
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError


CODE_LABEL_ID = "019e5910-a16d-7f09-8b59-79a8b0422923"
DEFAULT_LIMIT = 10
DEFAULT_PROJECT = "Agentis"
DEFAULT_STATE_FILE = Path(__file__).resolve().parents[1] / ".agentis" / "label-code-tasks-state.json"
CODE_EDIT_TOOLS = {
    "apply_patch",
    "edit",
    "edit_file",
    "file_edit",
    "multiedit",
    "multi_edit",
    "notebookedit",
    "str_replace",
    "str_replace_editor",
    "update_file",
    "write",
    "write_file",
}
PATH_KEYS = {"file", "filepath", "file_path", "filename", "path", "paths", "relative_path"}
PATCH_FILE_PREFIXES = ("*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Label Agentis tasks whose runs modified code.")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help=f"Exact project name. Default: {DEFAULT_PROJECT}")
    parser.add_argument("--project-id", help="Project UUID. If set, project lookup by name is skipped.")
    parser.add_argument("--endpoint", help="Agentis base URL. Defaults to AGENTIS_ENDPOINT/.env settings.")
    parser.add_argument("--token", help="Agentis API token. Defaults to AGENTIS_TOKEN/.env settings.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Page size. Default: {DEFAULT_LIMIT}")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE, help="Path to the state JSON file.")
    parser.add_argument("--from-number", type=int, help="Override state and process tasks with a greater number.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Only print matching tasks; do not save labels or state."
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


def load_last_processed_number(state_file: Path) -> int:
    if not state_file.is_file():
        return 0
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read state file {state_file}: {exc}") from exc
    value = payload.get("last_processed_number") if isinstance(payload, dict) else None
    return value if isinstance(value, int) else 0


def save_last_processed_number(state_file: Path, number: int) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"last_processed_number": number}, indent=2) + "\n", encoding="utf-8")


def fetch_tasks(client: AgentisJsonRpcClient, project_id: str, limit: int, number_after: int) -> list[dict[str, Any]]:
    return list(
        paged_get_list(
            client,
            "task.get_list",
            {"project": project_id, "number_after": number_after},
            limit=limit,
            sort={"column": "number", "direction": "asc"},
        )
    )


def fetch_task_detail(client: AgentisJsonRpcClient, task_id: str) -> dict[str, Any]:
    result = client.call("task.fetch", {"id": task_id})
    if not isinstance(result, dict):
        raise RuntimeError(f"task.fetch returned an unexpected response for {task_id}: {result!r}")
    return result


def normalize_tool_name(tool: Any) -> str:
    if not isinstance(tool, str):
        return ""
    return tool.rsplit(".", 1)[-1].lower().replace("-", "_").strip()


def is_markdown_path(path: str) -> bool:
    return Path(path.strip()).suffix.lower() == ".md"


def patch_paths(patch_text: str) -> Iterator[str]:
    for line in patch_text.splitlines():
        for prefix in PATCH_FILE_PREFIXES:
            if line.startswith(prefix):
                yield line.removeprefix(prefix).strip()
                break


def extract_edit_paths(value: Any, *, key: str | None = None) -> Iterator[str]:
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            normalized_key = str(item_key).lower().replace("-", "_")
            if normalized_key in {"patch", "patch_text", "patchtext"} and isinstance(item_value, str):
                yield from patch_paths(item_value)
            else:
                yield from extract_edit_paths(item_value, key=normalized_key)
        return

    if isinstance(value, list):
        for item in value:
            yield from extract_edit_paths(item, key=key)
        return

    if isinstance(value, str) and key in {"state", "metadata", "input"}:
        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError:
            parsed_value = None
        if parsed_value is not None:
            yield from extract_edit_paths(parsed_value, key=key)
        return

    if isinstance(value, str) and key in PATH_KEYS:
        normalized = value.strip()
        if normalized:
            yield normalized


def edits_non_markdown_file(part: dict[str, Any]) -> bool:
    paths = list(extract_edit_paths(part))
    return any(not is_markdown_path(path) for path in paths)


def run_modified_code(runs: Any) -> bool:
    if not isinstance(runs, list):
        return False

    for run in runs:
        if not isinstance(run, dict):
            continue
        items = run.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            parts = item.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if (
                    isinstance(part, dict)
                    and normalize_tool_name(part.get("tool")) in CODE_EDIT_TOOLS
                    and edits_non_markdown_file(part)
                ):
                    return True
    return False


def extract_label_ids(form: dict[str, Any]) -> list[str]:
    label_ids: list[str] = []
    for label in form.get("labels") or []:
        if isinstance(label, dict):
            label_id = label.get("id")
        else:
            label_id = label
        if label_id:
            label_ids.append(str(label_id))
    return label_ids


def selector_id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("id")
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def build_task_save_payload(form: dict[str, Any], label_ids: list[str]) -> dict[str, Any]:
    model = form.get("model")
    model_payload: str | dict[str, Any] | None = None
    if isinstance(model, dict) and model.get("id"):
        model_payload = {"id": model["id"], "effort": model.get("effort")}
    elif model:
        model_payload = selector_id(model)

    return {
        "id": form.get("id"),
        "title": form.get("title") or "",
        "project": selector_id(form.get("project")),
        "agent": selector_id(form.get("agent")),
        "adapter": selector_id(form.get("adapter")),
        "environment": selector_id(form.get("environment")),
        "model": model_payload,
        "effort": form.get("effort") or (model.get("effort") if isinstance(model, dict) else None),
        "status": form.get("status"),
        "priority": form.get("priority"),
        "sprint": form.get("sprint"),
        "description": form.get("description"),
        "attachments": form.get("attachments") if isinstance(form.get("attachments"), list) else [],
        "adapter_options": form.get("adapter_options") if isinstance(form.get("adapter_options"), list) else [],
        "notice": form.get("notice"),
        "scheduled_at": form.get("scheduled_at"),
        "labels": label_ids,
        "related_tasks": [related_id for item in form.get("related_tasks") or [] if (related_id := selector_id(item))],
    }


def add_code_label(client: AgentisJsonRpcClient, detail: dict[str, Any]) -> bool:
    raw_form = detail.get("form")
    if not isinstance(raw_form, dict):
        raise RuntimeError(f"task.fetch returned detail without form: {detail!r}")

    label_ids = extract_label_ids(raw_form)
    if CODE_LABEL_ID in label_ids:
        return False

    result = client.call("task.save", {"data": build_task_save_payload(raw_form, [*label_ids, CODE_LABEL_ID])})
    if not isinstance(result, dict):
        raise RuntimeError(f"task.save returned an unexpected response for {raw_form.get('id')}: {result!r}")
    return True


def task_number(task: dict[str, Any]) -> int | None:
    value = task.get("number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def main() -> int:
    args = parse_args()
    settings = get_settings()
    endpoint = args.endpoint or settings.agentis_endpoint
    token = args.token or settings.agentis_token

    if not endpoint:
        print("Agentis endpoint is missing. Set AGENTIS_ENDPOINT or pass --endpoint.", file=sys.stderr)
        return 2

    try:
        last_processed_number = (
            args.from_number if args.from_number is not None else load_last_processed_number(args.state_file)
        )
        max_processed_number = last_processed_number
        labelled_count = 0
        checked_count = 0

        with AgentisJsonRpcClient(endpoint=endpoint, token=token) as client:
            project_id = args.project_id or resolve_project_id(client, args.project, args.limit)
            tasks = fetch_tasks(client, project_id, args.limit, last_processed_number)

            for task in tasks:
                number = task_number(task)
                if number is None:
                    continue

                task_id = str(task["id"])
                detail = fetch_task_detail(client, task_id)
                checked_count += 1
                max_processed_number = max(max_processed_number, number)

                if not run_modified_code(detail.get("runs")):
                    continue

                changed = False if args.dry_run else add_code_label(client, detail)
                if changed or args.dry_run:
                    labelled_count += 1
                    print(f"#{number}\t{task.get('title') or ''}\t{task_id}")

        if not args.dry_run and max_processed_number > last_processed_number:
            save_last_processed_number(args.state_file, max_processed_number)

        print(f"Checked {checked_count} new tasks, labelled {labelled_count}, last number {max_processed_number}.")
    except (AgentisJsonRpcError, RuntimeError, ValueError) as exc:
        print(f"Code label sync failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
