"""Integrace s binárkou `agentis-workflow` z venv (executor local); přeskočí se, pokud není nainstalovaná."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.workflow import manager as manager_module
from common.workflow.manager import WorkflowManager
from tests.support import find_engine_command

WORKFLOW_YAML = """\
version: 1
workflow:
  executor: local
  steps:
    - name: Greet
      run: |
        mkdir -p outputs
        printf 'Hotovo\\n' > outputs/comment.md
      outputs:
        - type: agent_comment
          bodyFrom: outputs/comment.md
          status: 4
"""


def test_real_engine_runs_local_workflow_and_reports_comment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    command = find_engine_command()
    if command is None:
        pytest.skip("agentis-workflow is not installed in this environment")
    # Git snapshoty nejsou předmětem testu — worktree v tmp není repozitář.
    monkeypatch.setattr(manager_module, "snapshot_sources_best_effort", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        manager_module,
        "write_changes_diff_best_effort",
        lambda *args, **kwargs: SimpleNamespace(status="skipped", diff_path=None),
    )
    worktree = tmp_path / "worktree"
    (worktree / ".agentis" / "workflows").mkdir(parents=True)
    (worktree / ".agentis" / "workflows" / "default.yaml").write_text(WORKFLOW_YAML, encoding="utf-8")
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    settings = Settings(
        host="127.0.0.1",
        port=8001,
        worktree_root=tmp_path / "worktrees",
        public_base_url=None,
        agentis_endpoint=None,
        agentis_token=None,
        project_run_root=tmp_path / "tmp-agentis",
        workflow_executor="local",
        bundled_workflow_dir=bundled,
        workflow_command=command,
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    manager = WorkflowManager(settings, agentis_sink=lambda method, params: calls.append((method, params)))
    context = AgentExecutionContextPayload.model_validate(
        {"run_id": "run-integration", "task_id": "task-int", "title": "Test", "adapter": {"runtime": "workflow"}}
    )

    result = manager.start_workflow(context, str(worktree), "Ahoj")

    assert result["executor"] == "local"
    assert result["steps"] == ["Greet"]
    assert manager.wait_idle(timeout=60) is True
    assert manager.run_result("task-int")["status"] == "success"
    comments = [params for method, params in calls if method == "task.add_agent_comment"]
    assert [comment["body"] for comment in comments] == ["Hotovo"]
