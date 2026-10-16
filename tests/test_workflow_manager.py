"""WorkflowManager nad falešným enginem (`fake_agentis_workflow.py`): spec, check, události, outputs, abort, approval."""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.workflow import manager as manager_module
from common.workflow.manager import (
    WorkflowApprovalError,
    WorkflowApprovalTimeoutError,
    WorkflowBusyError,
    WorkflowManager,
)

FAKE_ENGINE = Path(__file__).with_name("fake_agentis_workflow.py")
SERVICE_TOKEN = "svc-secret-value"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    bundled = tmp_path / "bundled-workflows"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "default.yaml").write_text("version: 1\n", encoding="utf-8")
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8001,
        "worktree_root": tmp_path / "worktrees",
        "public_base_url": None,
        "agentis_endpoint": None,
        "agentis_token": None,
        "agentis_service_token": SERVICE_TOKEN,
        "project_run_root": tmp_path / "tmp-agentis",
        "workflow_executor": "local",
        "bundled_workflow_dir": bundled,
        "workflow_command": (sys.executable, str(FAKE_ENGINE)),
    }
    values.update(overrides)
    return Settings(**values)


def _context(**overrides: Any) -> AgentExecutionContextPayload:
    payload: dict[str, Any] = {
        "run_id": "run-12345678",
        "task_id": "task-1",
        "title": "Test task",
        "task_number": 77,
        "working_dir": "/var/www/project",
        "adapter": {"runtime": "workflow", "model": "openai/gpt-5", "agent": "build", "effort": "low"},
    }
    payload.update(overrides)
    return AgentExecutionContextPayload.model_validate(payload)


def _worktree(tmp_path: Path, *names: str) -> Path:
    worktree = tmp_path / "worktree"
    for name in names or ("default",):
        path = worktree / ".agentis" / "workflows" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("version: 1\nworkflow:\n  steps: []\n", encoding="utf-8")
    return worktree


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    # Git snapshoty a diff nejsou předmětem testů manageru — stub místo reálného repozitáře.
    monkeypatch.setattr(manager_module, "snapshot_sources_best_effort", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        manager_module,
        "write_changes_diff_best_effort",
        lambda *args, **kwargs: SimpleNamespace(status="skipped", diff_path=None),
    )
    monkeypatch.delenv("FAKE_ENGINE_SCENARIO", raising=False)
    monkeypatch.delenv("FAKE_ENGINE_CHECK", raising=False)

    calls: list[tuple[str, dict[str, Any]]] = []
    lock = threading.Lock()

    def sink(method: str, params: dict[str, Any]) -> None:
        with lock:
            calls.append((method, params))

    settings = _settings(tmp_path)
    manager = WorkflowManager(settings, agentis_sink=sink)
    return SimpleNamespace(manager=manager, calls=calls, settings=settings, worktree=_worktree(tmp_path))


def _methods(calls: list[tuple[str, dict[str, Any]]], method: str) -> list[dict[str, Any]]:
    return [params for name, params in calls if name == method]


def _events(calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return _methods(calls, "run.adapter_event")


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached in time")


# ---------------------------------------------------------------------------
# start: spec, check, události a outputs
# ---------------------------------------------------------------------------


def test_start_returns_summary_and_applies_outputs(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "success")
    context = _context()

    result = harness.manager.start_workflow(context, str(harness.worktree), "Ahoj")

    assert result["action"] == "workflow_start"
    assert result["task_id"] == "task-1"
    assert result["executor"] == "local"
    assert result["workflow"] is None
    assert result["workflow_file"] == ".agentis/workflows/default.yaml"
    assert result["steps"] == ["Prepare", "Agent", "Deploy"]
    assert harness.manager.wait_idle(timeout=10) is True

    events = _events(harness.calls)
    assert [event["kind"] for event in events[:1]] == ["workflow"]
    assert events[0]["message"] == "Workflow started."
    step_events = [event for event in events if event["kind"] == "workflow_step"]
    assert [(event["status"], event["message"]) for event in step_events] == [
        ("started", "Prepare"),
        ("success", "Prepare"),
        ("started", "Agent"),
        ("success", "Agent"),
        ("skipped", "Step skipped (if: STEP_VAR == 'y'): Deploy"),
    ]
    assert step_events[0]["data"]["job"] == "fake-prepare"
    assert step_events[-1]["data"]["condition"] == "STEP_VAR == 'y'"
    assert events[-1]["status"] == "success"
    assert events[-1]["message"] == "Workflow finished."

    [comment] = _methods(harness.calls, "task.add_agent_comment")
    assert comment["body"] == "Hotovo; token=yes"
    assert comment["status"] == 4
    assert comment["author_name"] == "Bot"
    assert comment["attachments"][0] == {"label": "Preview", "value": "https://example.test/preview", "type": "url"}
    assert comment["actions"] == [
        {
            "title": "Git merge",
            "prompt": "Merge it",
            "adapter_method": "start",
            "workflow": "merge",
            "continue_previous_run": False,
        }
    ]
    assert comment["artifacts"] == [
        {
            "name": "report.txt",
            "filename": "report.txt",
            "content": base64.b64encode(b"report-body").decode("ascii"),
        }
    ]
    assert _methods(harness.calls, "run.store_session_id") == [{"run_id": "run-12345678", "session_id": "sess-1"}]
    assert harness.manager.run_result("task-1")["status"] == "success"


def test_spec_and_run_files_keep_secrets_out_of_disk(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "success")

    harness.manager.start_workflow(_context(), str(harness.worktree), "Ahoj")
    assert harness.manager.wait_idle(timeout=10) is True

    run_dir = Path(harness.manager.run_result("task-1")["run_dir"])
    spec_text = (run_dir / "spec.json").read_text(encoding="utf-8")
    spec = json.loads(spec_text)
    assert SERVICE_TOKEN not in spec_text
    assert spec["pass_env"] == ["AGENTIS_SERVICE_TOKEN"]
    assert spec["executor"] is None
    assert spec["executor_default"] == "local"
    assert spec["labels"] == {"agentis.task_id": "task-1", "agentis.run_id": "run-12345678"}
    assert spec["vars"]["TASK_TITLE"] == "Test task"
    assert "NAMESPACE" not in spec["vars"]
    assert spec["env"]["AGENTIS_TASK_ID"] == "task-1"
    assert (run_dir / "prompt.md").read_text(encoding="utf-8") == "Ahoj"
    assert (run_dir / "context.json").is_file()
    # Token dostal engine jen přes env child procesu (fake ho vrací v komentáři jako `token=yes`).
    assert SERVICE_TOKEN not in json.dumps(harness.calls)


def test_check_invalid_spec_raises_value_error_with_stderr(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_CHECK", "invalid")

    with pytest.raises(ValueError, match="invalid workflow: unknown key"):
        harness.manager.start_workflow(_context(), str(harness.worktree), "Ahoj")

    assert harness.manager.run_result("task-1") is None
    assert harness.calls == []


def test_check_busy_raises_workflow_busy(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_CHECK", "busy")

    with pytest.raises(WorkflowBusyError, match="busy"):
        harness.manager.start_workflow(_context(), str(harness.worktree), "Ahoj")

    assert harness.manager.run_result("task-1") is None


def test_missing_workflow_file_fails_before_engine_is_called(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    (settings.bundled_workflow_dir / "default.yaml").unlink()
    manager = WorkflowManager(settings, agentis_sink=lambda method, params: None)

    with pytest.raises(FileNotFoundError, match="no workflow file"):
        manager.start_workflow(_context(), str(_worktree(tmp_path, "other")), "Ahoj")


def test_named_workflow_writes_run_files_outside_worktree(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "success")
    worktree = _worktree(harness.worktree.parent, "merge")
    context = _context(adapter={"runtime": "workflow", "workflow": "merge"})

    result = harness.manager.start_workflow(context, str(worktree), "Merge")

    assert result["workflow"] == "merge"
    assert result["workflow_file"] == ".agentis/workflows/merge.yaml"
    assert harness.manager.wait_idle(timeout=10) is True
    run_dir = Path(harness.manager.run_result("task-1")["run_dir"])
    assert harness.settings.project_run_root in run_dir.parents
    # Pojmenovaná workflow nemají snapshot ani changes diff.
    assert harness.manager.snapshot_key_for_task("task-1") is None


def test_project_scope_uses_project_yaml_and_external_run_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "success")
    monkeypatch.setattr(manager_module, "snapshot_sources_best_effort", lambda *args, **kwargs: None)
    worktree = _worktree(tmp_path, "project")
    manager = WorkflowManager(_settings(tmp_path), agentis_sink=lambda method, params: None)
    context = _context(adapter={"runtime": "workflow", "scope": "project"})

    result = manager.start_workflow(context, str(worktree), "Projekt")

    assert result["workflow_file"] == ".agentis/workflows/project.yaml"
    assert manager.wait_idle(timeout=10) is True


def test_second_start_while_running_is_busy_and_abort_frees_task(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "block")
    harness.manager.start_workflow(_context(), str(harness.worktree), "Ahoj")
    run_dir = Path(harness.manager.run_result("task-1")["run_dir"])
    _wait_for(lambda: (run_dir / "ready").exists())

    with pytest.raises(WorkflowBusyError):
        harness.manager.start_workflow(_context(), str(harness.worktree), "Znovu")

    harness.manager.abort(_context())
    assert harness.manager.wait_idle(timeout=10) is True


# ---------------------------------------------------------------------------
# Selhání, abort a úklid
# ---------------------------------------------------------------------------


def test_failed_run_reports_failure_comment_without_followups(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "failed")

    harness.manager.start_workflow(_context(), str(harness.worktree), "Ahoj")
    assert harness.manager.wait_idle(timeout=10) is True

    run = harness.manager.run_result("task-1")
    assert run["status"] == "failed"
    [comment] = _methods(harness.calls, "task.add_agent_comment")
    assert comment["body"] == "Failure comment"
    assert comment["actions"] == []
    failed_step = [event for event in _events(harness.calls) if event["status"] == "failed"]
    assert [event["kind"] for event in failed_step] == ["workflow_step", "idle"]
    assert failed_step[0]["data"]["result"] == "failed"
    assert failed_step[0]["data"]["attempts"] == 2
    assert failed_step[0]["data"]["log_tail"] == "boom"
    assert failed_step[0]["data"]["continueOnError"] is False
    assert failed_step[1]["message"] == "Workflow selhalo."
    assert failed_step[1]["data"]["failed_step"] == "Agent"


def test_missing_result_marks_run_failed(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "no_result")

    harness.manager.start_workflow(_context(), str(harness.worktree), "Ahoj")
    assert harness.manager.wait_idle(timeout=10) is True

    run = harness.manager.run_result("task-1")
    assert run["status"] == "failed"
    assert "without a valid result.json" in run["error"]
    crashed = [event for event in _events(harness.calls) if event["message"] == "Workflow run failed."]
    assert crashed and crashed[0]["status"] == "failed"
    assert _methods(harness.calls, "task.add_agent_comment") == []


def test_abort_sends_sigterm_and_reports_aborted(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "block")
    harness.manager.start_workflow(_context(), str(harness.worktree), "Ahoj")
    run_dir = Path(harness.manager.run_result("task-1")["run_dir"])
    _wait_for(lambda: (run_dir / "ready").exists())

    result = harness.manager.abort(_context())

    assert result["action"] == "abort"
    assert result["deleted"] == "workflow engine stopped (exit 143)"
    assert harness.manager.wait_idle(timeout=10) is True
    assert harness.manager.run_result("task-1")["status"] == "aborted"
    assert _methods(harness.calls, "task.add_agent_comment") == []
    abort_events = [event for event in _events(harness.calls) if event["kind"] == "workflow_abort"]
    assert len(abort_events) == 1
    assert abort_events[0]["data"]["deleted"] == "workflow engine stopped (exit 143)"


def test_abort_without_running_engine_uses_label_cleanup(harness) -> None:
    result = harness.manager.abort(_context())

    assert "--label agentis.task_id=task-1" in result["deleted"]
    assert "--label agentis.run_id=run-12345678" in result["deleted"]
    assert "--executor local" in result["deleted"]


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def _approval_run(manager: WorkflowManager, approval_id: str) -> Any:
    entry = manager._approval_runs.get(approval_id)
    return entry.run if entry is not None else None


def _approval_context() -> AgentExecutionContextPayload:
    return _context(adapter={"runtime": "workflow", "workflow": "approval"})


def _approval_worktree(tmp_path: Path) -> Path:
    return _worktree(tmp_path, "approval")


@pytest.mark.parametrize(("approved", "expected"), [("1", 1), ("0", 0)])
def test_approve_returns_scalar_without_reporting(harness, monkeypatch, approved: str, expected: int) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "success")
    monkeypatch.setenv("FAKE_ENGINE_APPROVED", approved)
    worktree = _approval_worktree(harness.worktree.parent)

    result = harness.manager.approve(_approval_context(), str(worktree), "approval-1", {"command": "pytest"}, timeout=10)

    assert result == expected
    assert harness.calls == []
    entry = harness.manager._approval_runs["approval-1"]
    assert entry.run is not None and entry.run.report_to_agentis is False
    assert entry.run.task_label.startswith("approval-")
    spec = json.loads(entry.run.spec_path.read_text(encoding="utf-8"))
    assert spec["require_vars"] == ["APPROVED"]
    assert json.loads(spec["env"]["AGENTIS_APPROVE_METADATA"]) == {"command": "pytest"}


def test_approve_returns_metadata(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "success")
    monkeypatch.setenv("FAKE_ENGINE_APPROVED", "1")
    monkeypatch.setenv("FAKE_ENGINE_APPROVED_METADATA", json.dumps({"score": 0.1}))
    worktree = _approval_worktree(harness.worktree.parent)

    result = harness.manager.approve(_approval_context(), str(worktree), "approval-2", {"command": "ls"}, timeout=10)

    assert result == {"approved": 1, "resolved_metadata": {"score": 0.1}}


def test_approve_rejects_invalid_approved_value(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "success")
    monkeypatch.setenv("FAKE_ENGINE_APPROVED", "yes")
    worktree = _approval_worktree(harness.worktree.parent)

    with pytest.raises(WorkflowApprovalError, match="invalid APPROVED value"):
        harness.manager.approve(_approval_context(), str(worktree), "approval-3", {"command": "ls"}, timeout=10)


def test_approve_rejects_invalid_metadata(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "success")
    monkeypatch.setenv("FAKE_ENGINE_APPROVED", "1")
    monkeypatch.setenv("FAKE_ENGINE_APPROVED_METADATA", "[1, 2]")
    worktree = _approval_worktree(harness.worktree.parent)

    with pytest.raises(WorkflowApprovalError, match="invalid APPROVED_METADATA"):
        harness.manager.approve(_approval_context(), str(worktree), "approval-4", {"command": "ls"}, timeout=10)


def test_approve_is_idempotent_and_rejects_reused_id_with_other_input(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "success")
    monkeypatch.setenv("FAKE_ENGINE_APPROVED", "1")
    worktree = _approval_worktree(harness.worktree.parent)
    metadata = {"command": "pytest", "context": {"scope": "test"}}

    first = harness.manager.approve(_approval_context(), str(worktree), "approval-5", metadata, timeout=10)
    second = harness.manager.approve(_approval_context(), str(worktree), "approval-5", metadata, timeout=10)

    assert first == second == 1
    assert len(harness.manager._approval_runs) == 1
    with pytest.raises(WorkflowApprovalError, match="already used with different input"):
        harness.manager.approve(_approval_context(), str(worktree), "approval-5", {"command": "rm"}, timeout=10)


def test_approve_timeout_aborts_only_approval_run(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "block")
    worktree = _approval_worktree(harness.worktree.parent)

    with pytest.raises(WorkflowApprovalTimeoutError):
        harness.manager.approve(_approval_context(), str(worktree), "approval-6", {"command": "ls"}, timeout=2)

    assert harness.manager.wait_idle(timeout=10) is True
    entry = harness.manager._approval_runs["approval-6"]
    assert entry.run is not None
    assert entry.run.status == "aborted"
    assert harness.calls == []


def test_parent_abort_stops_running_approval(harness, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_SCENARIO", "block")
    worktree = _approval_worktree(harness.worktree.parent)
    errors: list[BaseException] = []

    def run_approval() -> None:
        try:
            harness.manager.approve(_approval_context(), str(worktree), "approval-7", {"command": "ls"}, timeout=30)
        except BaseException as exc:  # noqa: BLE001 - výjimku ověřuje hlavní vlákno
            errors.append(exc)

    thread = threading.Thread(target=run_approval)
    thread.start()
    _wait_for(lambda: _approval_run(harness.manager, "approval-7") is not None)
    run_dir = Path(_approval_run(harness.manager, "approval-7").run_dir)
    _wait_for(lambda: (run_dir / "ready").exists())

    harness.manager.abort(_context())
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], WorkflowApprovalError)
    assert "aborted" in str(errors[0])
