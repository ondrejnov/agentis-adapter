from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from app.cli import run
from common.config import get_settings
from common.workflow import devrun
from common.workflow.runtime import StepResult

DEMO_WORKFLOW = """\
version: 1
workflow:
  executor: local
  workingDir: "[%WORKDIR%]"
  steps:
    - name: Hello
      run: echo hello
      outputs:
        - type: agent_comment
          bodyFrom: outputs/comment.md
          status: done
          name: Demo
    - name: Send mail
      if: SEND_EMAIL
      run: echo mail
"""


class OutputWritingRunner:
    """Fake runner: krok `Hello` zapíše komentář, krok `Fail` selže."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def prepare(self, workflow, *, namespace: str, run_dir: Path) -> None:
        pass

    def has_active_run(self, namespace: str, task_label: str) -> bool:
        return False

    def run_step(self, workflow, step, *, run_dir: Path, env: dict[str, str], **kwargs: Any) -> StepResult:
        self.steps.append({"step": step.name, "env": dict(env)})
        if step.name == "Fail":
            return StepResult(status="failed", log_tail="boom\n")
        outputs = run_dir / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        prompt = Path(env["AGENTIS_PROMPT_FILE"]).read_text(encoding="utf-8")
        (outputs / "comment.md").write_text(f"# Výsledek\n\n{prompt}", encoding="utf-8")
        return StepResult(status="succeeded")

    def abort(self, namespace: str, labels: dict[str, str]) -> str:
        return "aborted"

    def delete_namespace(self, namespace: str) -> None:
        pass


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    workflows = project / ".agentis" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "demo.yaml").write_text(DEMO_WORKFLOW, encoding="utf-8")
    (workflows / "broken.yaml").write_text(DEMO_WORKFLOW.replace("name: Hello", "name: Fail"), encoding="utf-8")
    (workflows / "_base.yaml").write_text("version: 1\nworkflow:\n  workingDir: x\n", encoding="utf-8")
    monkeypatch.setenv("ADAPTER_PROJECT_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("ADAPTER_BUNDLED_WORKFLOW_DIR", str(tmp_path / "bundled"))
    get_settings.cache_clear()
    yield project
    get_settings.cache_clear()


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> OutputWritingRunner:
    fake = OutputWritingRunner()
    original = devrun.create_service
    monkeypatch.setattr(devrun, "create_service", lambda settings, **kwargs: original(settings, **kwargs, runner=fake))
    return fake


def test_find_project_walks_up_to_agentis_directory(project: Path) -> None:
    nested = project / "src" / "pkg"
    nested.mkdir(parents=True)

    assert devrun.find_project(nested) == project


def test_resolve_target_accepts_name_and_yaml_path(project: Path) -> None:
    assert devrun.resolve_target("demo", None, project / ".agentis") == (project, "demo")
    assert devrun.resolve_target(".agentis/workflows/demo.yaml", None, project) == (project, "demo")


def test_resolve_target_rejects_invalid_name_and_foreign_path(project: Path, tmp_path: Path) -> None:
    stray = tmp_path / "demo.yaml"
    stray.write_text(DEMO_WORKFLOW, encoding="utf-8")

    with pytest.raises(devrun.WorkflowCliError):
        devrun.resolve_target("../etc", None, project)
    with pytest.raises(devrun.WorkflowCliError):
        devrun.resolve_target(str(stray), None, project)


def test_build_start_payload_omits_model_and_effort_when_not_set(tmp_path: Path) -> None:
    payload = devrun.build_start_payload(working_dir=tmp_path, prompt="hi", workflow="demo", model=None, effort=None)

    assert payload["params"]["context"]["adapter"] == {
        "scope": "project",
        "runtime": "local",
        "agent": "build",
        "workflow": "demo",
    }


def test_validate_skips_base_files_and_reports_failures(project: Path) -> None:
    (project / ".agentis" / "workflows" / "bad.yaml").write_text("version: 1\nworkflow:\n  nope: 1\n", "utf-8")
    out = io.StringIO()

    code = devrun.validate_workflows([], None, project, out)

    text = out.getvalue()
    assert code == 1
    assert "OK   " in text and "demo.yaml" in text
    assert "FAIL" in text and "bad.yaml" in text
    assert "_base.yaml" not in text


def test_run_workflow_prints_steps_comment_and_run_dir(project: Path, runner: OutputWritingRunner) -> None:
    out = io.StringIO()

    code = devrun.run_workflow("demo", prompt="Napiš newsletter", cwd=project, out=out)

    text = out.getvalue()
    assert code == 0
    assert [step["step"] for step in runner.steps] == ["Hello"]
    assert "AGENTIS_MODEL" not in runner.steps[0]["env"]
    assert "▶ Hello" in text and "✓ Hello" in text
    assert "Krok přeskočen (if: SEND_EMAIL): Send mail" in text
    assert "autor: Demo, status: done" in text
    assert "│ Napiš newsletter" in text
    assert "✓ Workflow doběhlo" in text
    assert "run adresář:" in text


def test_run_workflow_returns_failure_with_log_tail(project: Path, runner: OutputWritingRunner) -> None:
    out = io.StringIO()

    code = devrun.run_workflow("broken", cwd=project, out=out)

    text = out.getvalue()
    assert code == 1
    assert "✗ Krok selhal (failed): Fail" in text
    assert "│ boom" in text
    assert "skončilo stavem failed" in text


def test_cli_workflow_run_unknown_workflow_exits_with_usage_error(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(project)

    with pytest.raises(SystemExit) as exc:
        run(["workflow", "run", "missing"])

    assert exc.value.code == 2
    assert "Workflow 'missing' nenalezeno" in capsys.readouterr().err


def test_cli_workflow_run_dispatches_to_devrun(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_workflow(target: str, **kwargs: Any) -> int:
        captured.update(kwargs, target=target)
        return 0

    monkeypatch.setattr(devrun, "run_workflow", fake_run_workflow)
    monkeypatch.chdir(project)

    with pytest.raises(SystemExit) as exc:
        run(["workflow", "run", "demo", "prompt text", "-m", "openai/x"])

    assert exc.value.code == 0
    assert captured["target"] == "demo"
    assert captured["prompt"] == "prompt text"
    assert captured["model"] == "openai/x"
    assert captured["runtime"] == "local"
    assert captured["agentis_callbacks"] is False
