from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentiscode.adapter import AgentisCodeAdapterService
from agentiscode.api import _DISPATCH, create_app
from agentiscode.session_manager import AgentisCodeSessionManager
from common.config import Settings
from common.models import AdapterOptionsPayload, AgentExecutionContextPayload
from tests.support import RpcTestClient


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8004,
        "default_namespace": "agentis",
        "app_host": None,
        "manifest_path": Path("/tmp/opencode.yaml"),
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
        "adapter": AdapterOptionsPayload(agent="build", model="openai/gpt-5", variant="high"),
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
