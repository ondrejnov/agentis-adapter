from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from scripts import label_code_tasks
from scripts import remove_code_labels


def test_run_modified_code_detects_namespaced_edit_tool() -> None:
    runs = [
        {
            "items": [
                {
                    "parts": [
                        {"type": "tool", "tool": "functions.read"},
                        {"type": "tool", "tool": "functions.apply_patch", "patchText": "*** Update File: app/main.py\n"},
                    ]
                }
            ]
        }
    ]

    assert label_code_tasks.run_modified_code(runs) is True


def test_run_modified_code_ignores_markdown_only_edits() -> None:
    runs = [
        {
            "items": [
                {
                    "parts": [
                        {"type": "tool", "tool": "write", "filePath": "README.md"},
                        {"type": "tool", "tool": "functions.apply_patch", "patchText": "*** Update File: docs/guide.md\n"},
                    ]
                }
            ]
        }
    ]

    assert label_code_tasks.run_modified_code(runs) is False


def test_run_modified_code_detects_mixed_markdown_and_non_markdown_edits() -> None:
    runs = [
        {
            "items": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "tool": "functions.apply_patch",
                            "patchText": "*** Update File: README.md\n*** Update File: scripts/sync.py\n",
                        },
                    ]
                }
            ]
        }
    ]

    assert label_code_tasks.run_modified_code(runs) is True


def test_run_modified_code_detects_paths_inside_serialized_tool_state() -> None:
    runs = [
        {
            "items": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "tool": "apply_patch",
                            "state": json.dumps(
                                {
                                    "input": {"patchText": "*** Update File: README.md\n"},
                                    "metadata": {"files": [{"filePath": "frontend/app/pages/chat.tsx"}]},
                                }
                            ),
                        }
                    ]
                }
            ]
        }
    ]

    assert label_code_tasks.run_modified_code(runs) is True


def test_run_modified_code_ignores_markdown_paths_inside_serialized_tool_state() -> None:
    runs = [
        {
            "items": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "tool": "apply_patch",
                            "state": json.dumps(
                                {
                                    "input": {"patchText": "*** Update File: README.md\n"},
                                    "metadata": {"files": [{"filePath": "docs/guide.md"}]},
                                }
                            ),
                        }
                    ]
                }
            ]
        }
    ]

    assert label_code_tasks.run_modified_code(runs) is False


def test_run_modified_code_ignores_non_edit_tools() -> None:
    runs = [{"items": [{"parts": [{"type": "tool", "tool": "read"}, {"type": "tool", "tool": "bash"}]}]}]

    assert label_code_tasks.run_modified_code(runs) is False


def test_add_code_label_preserves_existing_labels() -> None:
    calls = []

    class FakeClient:
        def call(self, method, params):
            calls.append((method, params))
            return {"form": params["data"]}

    changed = label_code_tasks.add_code_label(
        cast(Any, FakeClient()),
        {
            "form": {
                "id": "task-1",
                "title": "Task",
                "project": {"id": "project-1", "name": "Agentis"},
                "labels": [{"id": "existing-label", "text": "existing"}],
            }
        },
    )

    assert changed is True
    assert calls[0][0] == "task.save"
    payload = calls[0][1]["data"]
    assert payload["id"] == "task-1"
    assert payload["title"] == "Task"
    assert payload["project"] == "project-1"
    assert payload["labels"] == ["existing-label", label_code_tasks.CODE_LABEL_ID]


def test_remove_code_label_preserves_other_labels() -> None:
    calls = []

    class FakeClient:
        def call(self, method, params):
            calls.append((method, params))
            return {"form": params["data"]}

    changed = remove_code_labels.remove_code_label(
        cast(Any, FakeClient()),
        {
            "form": {
                "id": "task-1",
                "title": "Task",
                "project": {"id": "project-1", "name": "Agentis"},
                "labels": [
                    {"id": "existing-label", "text": "existing"},
                    {"id": label_code_tasks.CODE_LABEL_ID, "text": "Code"},
                ],
            }
        },
    )

    assert changed is True
    assert calls[0][0] == "task.save"
    payload = calls[0][1]["data"]
    assert payload["id"] == "task-1"
    assert payload["title"] == "Task"
    assert payload["project"] == "project-1"
    assert payload["labels"] == ["existing-label"]


def test_remove_code_label_ignores_tasks_without_code_label() -> None:
    class FakeClient:
        def call(self, method, params):
            raise AssertionError(f"Unexpected call: {method} {params}")

    changed = remove_code_labels.remove_code_label(
        cast(Any, FakeClient()),
        {"form": {"id": "task-1", "labels": [{"id": "existing-label"}]}},
    )

    assert changed is False


def test_main_stores_highest_checked_number(monkeypatch, tmp_path, capsys) -> None:
    state_file = tmp_path / "state.json"
    calls = []

    class FakeClient:
        def __init__(self, endpoint, token):
            assert endpoint == "http://agentis.local"
            assert token == "token"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def call(self, method, params):
            calls.append((method, params))
            if method == "project.get_list":
                return {"items": [{"id": "project-1", "name": "Agentis"}], "more": False}
            if method == "task.get_list":
                return {
                    "items": [
                        {"id": "task-1", "number": 10, "title": "No code"},
                        {"id": "task-2", "number": 11, "title": "Code"},
                    ],
                    "more": False,
                }
            if method == "task.fetch" and params["id"] == "task-1":
                return {"form": {"id": "task-1", "labels": []}, "runs": []}
            if method == "task.fetch" and params["id"] == "task-2":
                return {
                    "form": {"id": "task-2", "title": "Code", "labels": []},
                    "runs": [{"items": [{"parts": [{"tool": "write", "filePath": "app/main.py"}]}]}],
                }
            if method == "task.save":
                return {"form": params["data"]}
            raise AssertionError(f"Unexpected call: {method} {params}")

    monkeypatch.setattr(label_code_tasks, "AgentisJsonRpcClient", FakeClient)
    monkeypatch.setattr(
        label_code_tasks,
        "get_settings",
        lambda: SimpleNamespace(agentis_endpoint="http://agentis.local", agentis_token="token"),
    )
    monkeypatch.setattr(
        label_code_tasks,
        "parse_args",
        lambda: SimpleNamespace(
            project="Agentis",
            project_id=None,
            endpoint=None,
            token=None,
            limit=500,
            state_file=state_file,
            from_number=None,
            dry_run=False,
        ),
    )

    assert label_code_tasks.main() == 0

    assert label_code_tasks.load_last_processed_number(state_file) == 11
    assert any(method == "task.save" for method, _params in calls)
    assert "Checked 2 new tasks, labelled 1, last number 11." in capsys.readouterr().out
