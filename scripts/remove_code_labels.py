#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import get_settings
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from scripts.label_code_tasks import (
    CODE_LABEL_ID,
    DEFAULT_PROJECT,
    build_task_save_payload,
    extract_label_ids,
    fetch_task_detail,
    paged_get_list,
    resolve_project_id,
    task_number,
)


DEFAULT_LIMIT = 500


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remove the code label from Agentis tasks.")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help=f"Exact project name. Default: {DEFAULT_PROJECT}")
    parser.add_argument("--project-id", help="Project UUID. If set, project lookup by name is skipped.")
    parser.add_argument("--endpoint", help="Agentis base URL. Defaults to AGENTIS_ENDPOINT/.env settings.")
    parser.add_argument("--token", help="Agentis API token. Defaults to AGENTIS_TOKEN/.env settings.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Page size. Default: {DEFAULT_LIMIT}")
    parser.add_argument("--dry-run", action="store_true", help="Only print matching tasks; do not save changes.")
    return parser.parse_args()


def fetch_code_label_tasks(client: AgentisJsonRpcClient, project_id: str, limit: int) -> list[dict[str, Any]]:
    return list(
        paged_get_list(
            client,
            "task.get_list",
            {"project": project_id, "labels": CODE_LABEL_ID},
            limit=limit,
            sort={"column": "number", "direction": "asc"},
        )
    )


def remove_code_label(client: AgentisJsonRpcClient, detail: dict[str, Any]) -> bool:
    raw_form = detail.get("form")
    if not isinstance(raw_form, dict):
        raise RuntimeError(f"task.fetch returned detail without form: {detail!r}")

    label_ids = extract_label_ids(raw_form)
    if CODE_LABEL_ID not in label_ids:
        return False

    remaining_label_ids = [label_id for label_id in label_ids if label_id != CODE_LABEL_ID]
    result = client.call("task.save", {"data": build_task_save_payload(raw_form, remaining_label_ids)})
    if not isinstance(result, dict):
        raise RuntimeError(f"task.save returned an unexpected response for {raw_form.get('id')}: {result!r}")
    return True


def main() -> int:
    args = parse_args()
    settings = get_settings()
    endpoint = args.endpoint or settings.agentis_endpoint
    token = args.token or settings.agentis_token

    if not endpoint:
        print("Agentis endpoint is missing. Set AGENTIS_ENDPOINT or pass --endpoint.", file=sys.stderr)
        return 2

    try:
        removed_count = 0
        checked_count = 0

        with AgentisJsonRpcClient(endpoint=endpoint, token=token) as client:
            project_id = args.project_id or resolve_project_id(client, args.project, args.limit)
            tasks = fetch_code_label_tasks(client, project_id, args.limit)

            for task in tasks:
                task_id = str(task["id"])
                detail = fetch_task_detail(client, task_id)
                checked_count += 1

                changed = False if args.dry_run else remove_code_label(client, detail)
                if changed or args.dry_run:
                    removed_count += 1
                    number = task_number(task)
                    prefix = f"#{number}" if number is not None else "#?"
                    print(f"{prefix}\t{task.get('title') or ''}\t{task_id}")

        action = "would remove" if args.dry_run else "removed"
        print(f"Checked {checked_count} tasks with code label, {action} {removed_count} labels.")
    except (AgentisJsonRpcError, RuntimeError, ValueError) as exc:
        print(f"Code label removal failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
