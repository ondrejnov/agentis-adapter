"""Testy StatusRegistry a read-only status endpointů pro TUI `agentis-top`."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from common.models import AdapterOptionsPayload, AgentExecutionContextPayload
from common.status import activity_from_event, get_status_registry, reset_status_registry
from opencode.api import create_app


@pytest.fixture(autouse=True)
def clean_registry():
    yield reset_status_registry()
    reset_status_registry()


def make_context(**overrides: Any) -> AgentExecutionContextPayload:
    values: dict[str, Any] = {
        "run_id": "run-1",
        "task_id": "task-1",
        "task_number": 42,
        "title": "Oprava exportu",
    }
    values.update(overrides)
    return AgentExecutionContextPayload(**values)


def test_run_lifecycle_in_snapshot():
    registry = get_status_registry()
    registry.run_received(make_context(), kind="agent", method="start")
    registry.run_update("run-1", session_id="ses-1", worktree="/tmp/wt")
    registry.run_activity("run-1", "Git worktree je připravený.")

    snapshot = registry.snapshot()
    assert snapshot["stats"]["runs_received"] == 1
    assert snapshot["stats"]["runs_running"] == 1
    [running] = snapshot["runs"]["running"]
    assert running["task_number"] == 42
    assert running["session_id"] == "ses-1"
    assert running["last_activity"] == "Git worktree je připravený."
    assert running["duration_seconds"] >= 0

    registry.run_finished("run-1", "success")
    snapshot = registry.snapshot()
    assert snapshot["stats"]["runs_running"] == 0
    assert snapshot["stats"]["runs_succeeded"] == 1
    assert snapshot["stats"]["avg_run_duration_seconds"] is not None
    [finished] = snapshot["runs"]["finished"]
    assert finished["status"] == "success"
    assert finished["finished_at"] is not None


def test_run_finished_is_idempotent_and_normalizes_status():
    registry = get_status_registry()
    registry.run_received(make_context(), kind="agent", method="start")
    registry.run_finished("run-1", "weird-status")
    registry.run_finished("run-1", "success")

    stats = registry.snapshot()["stats"]
    assert stats["runs_failed"] == 1
    assert stats["runs_succeeded"] == 0


def test_workflow_run_uses_workflow_name():
    context = make_context(adapter=AdapterOptionsPayload(workflow="merge"))
    registry = get_status_registry()
    registry.run_received(context, kind="workflow", method="start")

    [running] = registry.snapshot()["runs"]["running"]
    assert running["kind"] == "workflow"
    assert running["workflow"] == "merge"


def test_websocket_state_and_reconnect_counter():
    registry = get_status_registry()
    registry.ws_connecting("wss://agentis/ws", attempt=1)
    registry.ws_connected("wss://agentis/ws")
    registry.ws_disconnected("wss://agentis/ws", "ConnectionClosedError")
    registry.ws_connected("wss://agentis/ws")

    snapshot = registry.snapshot()
    assert snapshot["websocket"]["state"] == "connected"
    assert snapshot["stats"]["ws_reconnects"] == 1


def test_log_ring_buffer_scrubs_tokens_and_supports_cursor():
    registry = get_status_registry()
    registry.log("INFO", "první", {"task_id": "task-1", "agentis_token": "secret"})
    registry.log("WARN", "druhá", {})

    entries = registry.log_entries()
    assert [entry["message"] for entry in entries] == ["první", "druhá"]
    assert "agentis_token" not in entries[0]["fields"]
    assert registry.log_entries(after=entries[0]["seq"]) == [entries[1]]


def test_run_activity_log_cursor_and_unknown_run():
    registry = get_status_registry()
    registry.run_activity("run-1", "Edit app/main.py")
    registry.run_activity("run-1", "Bash pytest -q")

    entries = registry.run_log_entries("run-1")
    assert entries is not None
    assert [entry["text"] for entry in entries] == ["Edit app/main.py", "Bash pytest -q"]
    assert registry.run_log_entries("run-1", after=entries[0]["seq"]) == [entries[1]]
    assert registry.run_log_entries("missing") is None


def test_activity_from_event_formats():
    assert activity_from_event("session_start", {"model": "claude-fable-5"}) == "session start (claude-fable-5)"
    assert activity_from_event("tool_use", {"name": "Edit", "input": {"file_path": "app/main.py"}}) == "Edit app/main.py"
    assert activity_from_event("thinking", {}) == "přemýšlí…"
    assert activity_from_event("error", {"message": "boom"}) == "chyba: boom"
    assert activity_from_event("raw", {"line": "noise"}) is None
    assert activity_from_event("text", {"text": ""}) is None


def test_status_endpoints():
    registry = get_status_registry()
    registry.set_meta(adapter="opencode", adapter_id="adapter-1")
    registry.run_received(make_context(), kind="agent", method="start")
    registry.run_activity("run-1", "Edit app/main.py")
    registry.log("INFO", "hello", {})

    client = TestClient(create_app())

    status = client.get("/status").json()
    assert status["adapter"] == "opencode"
    assert status["websocket"]["state"] == "disconnected"
    assert status["runs"]["running"][0]["run_id"] == "run-1"

    log = client.get("/log").json()
    assert [entry["message"] for entry in log["entries"]] == ["hello"]

    run_log = client.get("/runs/run-1/log").json()
    assert [entry["text"] for entry in run_log["entries"]] == ["Edit app/main.py"]

    assert client.get("/runs/missing/log").status_code == 404
