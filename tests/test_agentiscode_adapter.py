from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentiscode.adapter import AgentisCodeAdapterService
from agentiscode.api import _DISPATCH, create_app
from agentiscode.session_manager import AgentisCodeSessionManager, _AgentisCodeSession
from common.config import Settings
from common.models import AdapterOptionsPayload, AgentExecutionContextPayload
from tests.support import RpcTestClient


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8004,
        "worktree_root": Path("/var/www/worktrees"),
        "public_base_url": "http://adapter.internal:8004",
        "agentis_endpoint": "http://agentis.local",
        "agentis_token": "secret-token",
        "agentiscode_command": "/usr/local/bin/agentiscode",
        "agentiscode_adapter": "opencode",
    }
    values.update(overrides)
    return Settings(**values)


def make_context(**overrides: Any) -> AgentExecutionContextPayload:
    payload: dict[str, Any] = {
        "run_id": "run-1",
        "task_id": "task-1",
        "title": "Implementace nove funkce",
        "description": "Popis ukolu",
        "project_slug": "agentis",
        "working_dir": "/var/www/repo",
        "adapter": AdapterOptionsPayload(agent="build", model="openai/gpt-5", effort="high"),
    }
    payload.update(overrides)
    return AgentExecutionContextPayload(**payload)


def test_session_manager_builds_agentiscode_command_with_telemetry() -> None:
    manager = AgentisCodeSessionManager(settings=make_settings())

    args = manager._build_args(make_context(), "/srv/worktrees/task-1", "Udelej X", None)

    assert args == [
        "/usr/local/bin/agentiscode",
        "--adapter",
        "opencode",
        "--cwd",
        "/srv/worktrees/task-1",
        "--json",
        "--task-id",
        "task-1",
        "--run-id",
        "run-1",
        "--task-status",
        "4",
        "--agentis-api",
        "http://agentis.local",
        "--agentis-token",
        "secret-token",
        "--model",
        "openai/gpt-5",
        "--effort",
        "high",
        "--agent",
        "build",
        "Udelej X",
    ]


def test_context_runtime_can_select_underlying_agentiscode_adapter() -> None:
    manager = AgentisCodeSessionManager(settings=make_settings(agentiscode_adapter="opencode"))
    context = make_context(adapter=AdapterOptionsPayload(runtime="claude", agent="build"))

    args = manager._build_args(context, "/srv/worktrees/task-1", "Udelej X", "ses-1")

    assert args[args.index("--adapter") + 1] == "claude"
    assert args[-3:] == ["--resume", "ses-1", "Udelej X"]


def test_session_manager_collects_top_level_agentiscode_text() -> None:
    sess = _AgentisCodeSession(session_id="sess-1", context=make_context(), worktree="/tmp/worktree")

    AgentisCodeSessionManager._consume_output_event(sess, {"type": "text", "text": "Hot"})
    AgentisCodeSessionManager._consume_output_event(sess, {"type": "text", "text": "ovo."})

    assert sess.final_text_chunks == ["Hot", "ovo."]


def test_session_manager_keeps_only_last_agentiscode_text_message() -> None:
    sess = _AgentisCodeSession(session_id="sess-1", context=make_context(), worktree="/tmp/worktree")

    AgentisCodeSessionManager._consume_output_event(sess, {"type": "text", "text": "Prvni odpoved."})
    AgentisCodeSessionManager._consume_output_event(sess, {"type": "tool", "id": "t1", "status": "running"})
    AgentisCodeSessionManager._consume_output_event(sess, {"type": "text", "text": "Fin"})
    AgentisCodeSessionManager._consume_output_event(sess, {"type": "text", "text": "alni odpoved."})

    assert sess.final_text_chunks == ["Fin", "alni odpoved."]


def test_session_manager_posts_completion_comment_with_attachments(monkeypatch, tmp_path: Path) -> None:
    captured_calls: list[dict[str, Any]] = []
    diff_path = tmp_path / ".changes.diff"
    diff_content = "diff -ruN before/app.py after/app.py\n-old\n+new\n"
    diff_path.write_text(diff_content, encoding="utf-8")
    manager = AgentisCodeSessionManager(settings=make_settings())
    context = make_context(
        project_github_repo="example/repo",
        adapter=AdapterOptionsPayload(agent="build", model="openai/gpt-5", task_status=7),
    )
    sess = _AgentisCodeSession(
        session_id="sess-1",
        context=context,
        worktree=str(tmp_path),
        snapshot_key="snap-1",
        final_text_chunks=["Ho", "tovo."],
    )

    monkeypatch.setattr(
        manager,
        "_agentis_call",
        lambda method, params: captured_calls.append({"method": method, "params": params}),
    )
    monkeypatch.setattr(
        manager,
        "_finish_session_actions",
        lambda sess: [
            {"label": "Pull Request", "value": "https://github.com/example/repo/pull/42/changes", "type": "url"}
        ],
    )
    monkeypatch.setattr(
        "agentiscode.session_manager.collect_screenshot_images",
        lambda project_root: [{"name": "result.png", "content": "cG5n"}] if project_root == str(tmp_path) else [],
    )
    monkeypatch.setattr(
        "agentiscode.session_manager.collect_expected_artifacts",
        lambda context, project_root: [{"path": "build.log"}] if project_root == str(tmp_path) else [],
    )
    monkeypatch.setattr(
        "agentiscode.session_manager.write_changes_diff_best_effort",
        lambda worktree, snapshot_key, label: type(
            "Result",
            (),
            {"status": "success", "diff_path": str(diff_path)},
        )(),
    )

    manager._finish_agentiscode_session(sess)

    assert captured_calls == [
        {
            "method": "task.add_agent_comment",
            "params": {
                "session_id": "sess-1",
                "body": "Hotovo.",
                "attachments": [
                    {
                        "label": "Pull Request",
                        "value": "https://github.com/example/repo/pull/42/changes",
                        "type": "url",
                    },
                    {"label": "Changes diff", "value": diff_content, "type": "diff"},
                ],
                "images": [{"name": "result.png", "content": "cG5n"}],
                "artifacts": [{"path": "build.log"}],
                "status": 7,
                "comment_type": "primary",
                "actions": AgentisCodeSessionManager._completion_actions(context, sess.worktree),
            },
        },
        {
            "method": "run.adapter_event",
            "params": {
                "run_id": "run-1",
                "kind": "idle",
                "status": "success",
                "event_id": "idle:sess-1:agentiscode-finish",
                "message": "agentiscode session doběhla.",
                "data": {"session_id": "sess-1"},
            },
        },
    ]


def test_agentiscode_runs_locally(monkeypatch) -> None:
    manager = MagicMock(spec=AgentisCodeSessionManager)
    manager.start.return_value = "ses_local"
    monkeypatch.setattr(AgentisCodeAdapterService, "_persist_agentis_session_id", lambda self, session_id: None)
    context = make_context(adapter=AdapterOptionsPayload(runtime="claude", agent="build"))
    adapter = AgentisCodeAdapterService(context=context, settings=make_settings(), session_manager=manager)

    deploy_result = adapter.deploy()
    adapter.start_session()

    assert deploy_result["status"] == "skipped"
    assert deploy_result["reason"] == "agentiscode_local"
    assert "kubectl_target" not in manager.start.call_args.kwargs


@pytest.fixture()
def agentiscode_client(monkeypatch):
    fake_manager = MagicMock(spec=AgentisCodeSessionManager)
    fake_manager.start.return_value = "ses_agentiscode"
    fake_manager.get_snapshot_key.return_value = None

    monkeypatch.setattr("agentiscode.api.AgentisCodeSessionManager", lambda settings: fake_manager)
    monkeypatch.setattr("agentiscode.api.get_settings", lambda: make_settings())
    monkeypatch.setattr(AgentisCodeAdapterService, "_persist_agentis_session_id", lambda self, session_id: None)
    monkeypatch.setattr(
        AgentisCodeAdapterService,
        "create_worktree",
        lambda self: {
            "action": "create_worktree",
            "task_id": self.context.task_id,
            "branch": "task-task-1",
            "base_branch": "master",
            "working_dir": "/srv/worktrees/task-1",
            "status": "created",
        },
    )

    app = create_app()
    return RpcTestClient(app, _DISPATCH), fake_manager


def test_start_dispatches_to_agentiscode_session_manager(agentiscode_client) -> None:
    client, manager = agentiscode_client

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "start",
            "params": {
                "context": {
                    "run_id": "run-1",
                    "task_id": "task-1",
                    "title": "Implementace nove funkce",
                    "description": "Popis ukolu",
                    "project_slug": "agentis",
                    "working_dir": "/var/www/repo",
                    "adapter": {"agent": "build", "model": "openai/gpt-5"},
                }
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()["result"]
    steps = [step["action"] for step in payload["adapter"]["steps"]]
    assert steps == ["create_worktree", "deploy", "wait_ready", "start_session"]
    assert payload["adapter"]["steps"][1]["reason"] == "agentiscode_local"
    assert payload["adapter"]["steps"][2]["url"] == "local://agentiscode"
    assert payload["adapter"]["steps"][3]["session_id"] == "ses_agentiscode"
    manager.start.assert_called_once()
