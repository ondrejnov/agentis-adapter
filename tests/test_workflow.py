"""Testy deklarativního Kubernetes workflow režimu."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from common.config import Settings
from common.models import AddMessageParams, AgentExecutionContextPayload, StartParams, AbortParams, QuestionParams
from common.rpc.jsonrpc import AgentJsonRpcException, AgentJsonRpcService
from common.workflow.manager import WorkflowBusyError, WorkflowManager
from common.workflow.runtime import build_bash_wrapper, build_job_manifest, job_labels, job_name
from common.workflow.schema import (
    PROJECT_WORKFLOW_FILE_RELPATH,
    WORKFLOW_FILE_RELPATH,
    WorkflowConditionError,
    WorkflowFile,
    WorkflowInterpolationError,
    evaluate_condition,
    interpolate_tokens,
    load_workflow_file,
)


WORKFLOW_YAML = """
version: 1
workflow:
  image: registry.example/agent:1.0
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 120
  ttlSecondsAfterFinished: 60
  envFiles:
    - /root/.config/agentis/agentis.env
  env:
    HOME: /root
    IS_SANDBOX: 1
    MAIN_DIR: "[%MAIN_DIR%]"
  volumeMounts:
    - name: www
      mountPath: /var/www
  steps:
    - name: Run agent
      run: |
        mkdir -p .agentis/outputs
        agentiscode < "$AGENTIS_PROMPT_FILE"
      outputs:
        - type: agent_comment
          bodyFrom: .agentis/outputs/final-comment.md
          status: 4
        - type: session_id
          valueFrom: .agentis/outputs/session-id
    - name: Create pull request
      image: registry.example/other:2.0
      timeoutSeconds: 30
      run: echo hotovo
      outputs:
        - type: url
          label: Pull Request
          valueFrom: .agentis/outputs/pull-request-url
volumes:
  - name: www
    hostPath:
      path: /var/www
"""


PROJECT_WORKFLOW_YAML = """
version: 1
workflow:
  image: registry.example/agent:1.0
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 120
  steps:
    - name: Run agent
      run: |
        mkdir -p "$AGENTIS_RUN_DIR/outputs"
        agentiscode < "$AGENTIS_PROMPT_FILE"
      outputs:
        - type: agent_comment
          bodyFrom: outputs/final-comment.md
          status: 5
        - type: session_id
          valueFrom: outputs/session-id
"""


CONDITIONAL_WORKFLOW_YAML = """
version: 1
workflow:
  image: registry.example/agent:1.0
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 120
  steps:
    - name: Check environment
      run: check-env
      outputs:
        - type: var
          name: ENV_READY
          valueFrom: .agentis/outputs/env-ready
    - name: Install dependencies
      if: ENV_READY != 'true'
      run: poetry install
      outputs:
        - type: url
          label: Install Log
          valueFrom: .agentis/outputs/install-log-url
    - name: Run agent
      run: agentiscode < "$AGENTIS_PROMPT_FILE"
      outputs:
        - type: agent_comment
          bodyFrom: .agentis/outputs/final-comment.md
          status: 4
"""


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8001,
        worktree_root=tmp_path / "worktrees",
        public_base_url=None,
        agentis_endpoint=None,
        agentis_token=None,
        project_run_root=tmp_path / "tmp-agentis",
    )


def _context(**overrides: Any) -> AgentExecutionContextPayload:
    payload: dict[str, Any] = {
        "run_id": "run-12345678",
        "task_id": "task-77",
        "title": "Test task",
        "task_number": 77,
        "working_dir": "/var/www/project",
        "adapter": {"runtime": "workflow", "model": "openai/gpt-5", "agent": "build", "effort": "low"},
    }
    payload.update(overrides)
    return AgentExecutionContextPayload.model_validate(payload)


def _write_workflow(worktree: Path) -> Path:
    path = worktree / WORKFLOW_FILE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKFLOW_YAML, encoding="utf-8")
    return path


def _values(worktree: Path) -> dict[str, str]:
    return {
        "NAMESPACE": "task-77-test",
        "WORKDIR": str(worktree),
        "MAIN_DIR": "/var/www/project",
        "RUN_ID": "run-12345678",
        "TASK_ID": "task-77",
        "TASK_NUMBER": "77",
        "TASK_TITLE": "Test task",
        "BRANCH": "task-task-77",
        "BASE_BRANCH": "master",
        "GITHUB_REPO": "org/repo",
    }


# ---------------------------------------------------------------------------
# Schema + interpolation
# ---------------------------------------------------------------------------


def test_workflow_schema_parses_and_interpolates(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    workflow = load_workflow_file(tmp_path / WORKFLOW_FILE_RELPATH, _values(tmp_path))

    assert isinstance(workflow, WorkflowFile)
    spec = workflow.workflow
    assert spec.workingDir == str(tmp_path)
    assert spec.env["IS_SANDBOX"] == "1"  # env hodnoty se koercují na string
    assert spec.env["MAIN_DIR"] == "/var/www/project"
    assert [step.name for step in spec.steps] == ["Run agent", "Create pull request"]
    assert spec.steps[1].image == "registry.example/other:2.0"
    assert spec.steps[0].outputs[0].type == "agent_comment"
    assert spec.steps[0].outputs[0].status == 4


def test_workflow_schema_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "ci.yaml"
    path.write_text(
        "version: 1\nworkflow:\n  image: x\n  parallel: true\n  steps:\n    - name: a\n      run: echo\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_workflow_file(path, _values(tmp_path))


def test_workflow_schema_parses_if_and_var_outputs(tmp_path: Path) -> None:
    path = tmp_path / "ci.yaml"
    path.write_text(CONDITIONAL_WORKFLOW_YAML, encoding="utf-8")
    workflow = load_workflow_file(path, _values(tmp_path))

    steps = workflow.workflow.steps
    assert steps[0].if_ is None
    assert steps[0].outputs[0].type == "var"
    assert steps[0].outputs[0].name == "ENV_READY"
    assert steps[1].if_ == "ENV_READY != 'true'"


def test_workflow_schema_rejects_invalid_condition_and_var_output(tmp_path: Path) -> None:
    path = tmp_path / "ci.yaml"
    path.write_text(
        "version: 1\nworkflow:\n  image: x\n  steps:\n    - name: a\n      if: 'A && B'\n      run: echo\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_workflow_file(path, _values(tmp_path))

    path.write_text(
        "version: 1\nworkflow:\n  image: x\n  steps:\n"
        "    - name: a\n      run: echo\n      outputs:\n        - type: var\n          valueFrom: out\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_workflow_file(path, _values(tmp_path))


def test_evaluate_condition_truthiness_negation_and_comparison() -> None:
    assert evaluate_condition("READY", {"READY": "1"})
    assert evaluate_condition("READY", {"READY": "yes"})
    assert not evaluate_condition("READY", {"READY": "false"})
    assert not evaluate_condition("READY", {"READY": "0"})
    assert not evaluate_condition("READY", {})
    assert evaluate_condition("!READY", {})
    assert not evaluate_condition("!READY", {"READY": "true"})
    assert evaluate_condition("MODE == 'fast'", {"MODE": "fast"})
    assert evaluate_condition('MODE == "a b"', {"MODE": "a b"})
    assert evaluate_condition("MODE == fast", {"MODE": "fast"})
    assert evaluate_condition("MODE != 'fast'", {"MODE": "slow"})
    assert not evaluate_condition("MODE != 'fast'", {"MODE": "fast"})
    # mezery kolem hodnoty ze souboru ořezává manager, ale i tak: neznámá proměnná == prázdno
    assert evaluate_condition("MODE != 'fast'", {})
    with pytest.raises(WorkflowConditionError):
        evaluate_condition("A && B", {})
    with pytest.raises(WorkflowConditionError):
        evaluate_condition("!A == 'x'", {})


def test_interpolation_replaces_allowlisted_tokens_and_rejects_unknown() -> None:
    values = {"WORKDIR": "/w", "TASK_TITLE": "Titulek"}
    assert interpolate_tokens("cd [%WORKDIR%] # [%TASK_TITLE%]", values) == "cd /w # Titulek"
    assert interpolate_tokens({"a": ["[%WORKDIR%]"]}, values) == {"a": ["/w"]}
    with pytest.raises(WorkflowInterpolationError):
        interpolate_tokens("[%EVIL_TOKEN%]", values)


# ---------------------------------------------------------------------------
# Job manifest + bash wrapper
# ---------------------------------------------------------------------------


def test_bash_wrapper_sets_pipefail_and_sources_env_files() -> None:
    wrapper = build_bash_wrapper(["/root/.config/agentis/agentis.env"], "echo ahoj")
    lines = wrapper.splitlines()
    assert lines[0] == "set -euo pipefail"
    assert ". /root/.config/agentis/agentis.env" in lines
    assert lines.index(". /root/.config/agentis/agentis.env") < lines.index("echo ahoj")
    assert 'cd "$WORKDIR"' in lines


def test_job_manifest_generation(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    workflow = load_workflow_file(tmp_path / WORKFLOW_FILE_RELPATH, _values(tmp_path))
    labels = job_labels(task_id="task-77", run_id="run-12345678", attempt_id="abcd1234", step_index=1, step_name="Create pull request")
    name = job_name("run-12345678", "abcd1234", 1, "Create pull request")
    manifest = build_job_manifest(
        workflow,
        workflow.workflow.steps[1],
        namespace="task-77-test",
        name=name,
        labels=labels,
        env={"AGENTIS_RUN_ID": "run-12345678", "WORKDIR": str(tmp_path)},
    )

    assert name.startswith("wf-run12345-abcd1234-1-")
    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["labels"]["agentis.workflow"] == "true"
    assert manifest["metadata"]["labels"]["agentis.step_index"] == "1"
    spec = manifest["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] == 30  # step-level override
    assert spec["ttlSecondsAfterFinished"] == 60
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["volumes"][0]["name"] == "www"
    container = pod["containers"][0]
    assert container["image"] == "registry.example/other:2.0"
    assert container["command"][:2] == ["/bin/bash", "-lc"]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["AGENTIS_RUN_ID"] == "run-12345678"
    assert env["HOME"] == "/root"
    assert "AGENTIS_TOKEN" not in env


# ---------------------------------------------------------------------------
# Workflow manager (fake kubectl runner)
# ---------------------------------------------------------------------------


class FakeRunner:
    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, dict[str, str]]] = []
        self.namespaces: list[str] = []
        self.results: list[str] = []
        self.release = threading.Event()
        self.release.set()
        self.log_tail = "boom log"

    def ensure_namespace(self, namespace: str) -> None:
        self.namespaces.append(namespace)

    def apply_job(self, manifest: dict[str, Any]) -> None:
        self.applied.append(manifest)

    def wait_for_job(self, namespace: str, name: str, *, timeout: float, abort_event=None, interval: float = 0.0) -> str:
        self.release.wait(timeout=5.0)
        return self.results.pop(0) if self.results else "succeeded"

    def job_log_tail(self, namespace: str, name: str, *, lines: int = 50) -> str:
        return self.log_tail

    def delete_jobs_by_labels(self, namespace: str, labels: dict[str, str]) -> str:
        self.deleted.append((namespace, labels))
        return "job deleted"

    def has_active_jobs(self, namespace: str, task_label: str) -> bool:
        return False


def _manager(tmp_path: Path, runner: FakeRunner) -> tuple[WorkflowManager, list[tuple[str, dict[str, Any]]]]:
    manager = WorkflowManager(_settings(tmp_path), runner=runner)
    calls: list[tuple[str, dict[str, Any]]] = []
    manager._agentis_call = lambda method, params: calls.append((method, params))  # type: ignore[method-assign]
    return manager, calls


def _wait_done(manager: WorkflowManager, task_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = manager._runs.get(task_id)
        if run is not None and run.thread is not None and not run.thread.is_alive():
            return
        time.sleep(0.01)
    raise AssertionError("workflow thread did not finish in time")


def test_start_workflow_runs_in_background_and_applies_outputs(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    _write_workflow(worktree)
    outputs_dir = worktree / ".agentis" / "outputs"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "final-comment.md").write_text("Hotovo, vše funguje.", encoding="utf-8")
    (outputs_dir / "session-id").write_text("ses_42\n", encoding="utf-8")
    (outputs_dir / "pull-request-url").write_text("https://github.com/org/repo/pull/1\n", encoding="utf-8")

    runner = FakeRunner()
    runner.release.clear()  # workflow zůstane "běžet", dokud test nepovolí dokončení
    manager, calls = _manager(tmp_path, runner)
    context = _context()

    result = manager.start_workflow(context, str(worktree), "udelej X")
    assert result["action"] == "workflow_start"
    assert result["steps"] == ["Run agent", "Create pull request"]

    # start je neblokující — Joby ještě neskončily, ale odpověď už máme
    prompt_file = worktree / ".agentis" / "runs" / result["attempt"] / "prompt.md"
    assert prompt_file.read_text(encoding="utf-8") == "udelej X"
    context_file = worktree / ".agentis" / "runs" / result["attempt"] / "context.json"
    assert json.loads(context_file.read_text(encoding="utf-8"))["task_id"] == "task-77"

    # běžící workflow pro stejný task blokuje další start
    with pytest.raises(WorkflowBusyError):
        manager.start_workflow(_context(), str(worktree), "druhy pokus")

    runner.release.set()
    _wait_done(manager, context.task_id)

    # outputs se aplikovaly až po úspěchu celého workflow
    methods = [method for method, _ in calls]
    assert "run.store_session_id" in methods
    comment_calls = [params for method, params in calls if method == "task.add_agent_comment"]
    assert len(comment_calls) == 1
    comment = comment_calls[0]
    assert comment["body"] == "Hotovo, vše funguje."
    assert comment["status"] == 4
    assert comment["attachments"] == [
        {"label": "Pull Request", "value": "https://github.com/org/repo/pull/1", "type": "url"}
    ]
    assert any(method == "run.adapter_event" and params["kind"] == "idle" and params["status"] == "success" for method, params in calls)

    # prompt ani token nejsou v žádném Job manifestu
    assert len(runner.applied) == 2
    for manifest in runner.applied:
        dumped = json.dumps(manifest)
        assert "udelej X" not in dumped
        env = {item["name"]: item["value"] for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["AGENTIS_PROMPT_FILE"] == str(prompt_file)
        assert env["AGENTIS_RUN_ID"] == "run-12345678"
        assert env["AGENTIS_MODEL"] == "openai/gpt-5"
        assert "AGENTIS_TOKEN" not in env


def test_conditional_step_is_skipped_and_vars_flow_into_env(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    path = worktree / WORKFLOW_FILE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONDITIONAL_WORKFLOW_YAML, encoding="utf-8")
    outputs_dir = worktree / ".agentis" / "outputs"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "env-ready").write_text("true\n", encoding="utf-8")
    (outputs_dir / "final-comment.md").write_text("Hotovo.", encoding="utf-8")
    # pozůstatek z minulého běhu — output přeskočeného kroku se nesmí aplikovat
    (outputs_dir / "install-log-url").write_text("https://example.org/stale", encoding="utf-8")

    runner = FakeRunner()
    manager, calls = _manager(tmp_path, runner)
    context = _context()

    manager.start_workflow(context, str(worktree), "udelej X")
    _wait_done(manager, context.task_id)

    assert manager._runs[context.task_id].status == "success"

    # prostřední krok se nespustil jako Job
    step_indexes = [manifest["metadata"]["labels"]["agentis.step_index"] for manifest in runner.applied]
    assert step_indexes == ["0", "2"]

    # proměnná z prvního kroku je env pro kroky po něm
    env = {item["name"]: item["value"] for item in runner.applied[1]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["ENV_READY"] == "true"

    # přeskočení se reportuje jako workflow_step se statusem skipped
    skip_events = [
        params
        for method, params in calls
        if method == "run.adapter_event" and params["kind"] == "workflow_step" and params["data"].get("skipped")
    ]
    assert len(skip_events) == 1
    assert skip_events[0]["status"] == "skipped"
    assert skip_events[0]["data"]["step"] == "Install dependencies"
    assert skip_events[0]["data"]["condition"] == "ENV_READY != 'true'"

    # outputs přeskočeného kroku se neaplikují, ostatní ano
    comment_calls = [params for method, params in calls if method == "task.add_agent_comment"]
    assert len(comment_calls) == 1
    assert comment_calls[0]["body"] == "Hotovo."
    assert comment_calls[0]["attachments"] == []


def test_conditional_step_runs_when_condition_holds(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    path = worktree / WORKFLOW_FILE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONDITIONAL_WORKFLOW_YAML, encoding="utf-8")
    outputs_dir = worktree / ".agentis" / "outputs"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "env-ready").write_text("false", encoding="utf-8")

    runner = FakeRunner()
    manager, _calls = _manager(tmp_path, runner)
    context = _context()

    manager.start_workflow(context, str(worktree), "udelej X")
    _wait_done(manager, context.task_id)

    step_indexes = [manifest["metadata"]["labels"]["agentis.step_index"] for manifest in runner.applied]
    assert step_indexes == ["0", "1", "2"]


def test_project_scope_uses_project_yaml_and_run_dir_outside_project(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    workflow_path = project_dir / PROJECT_WORKFLOW_FILE_RELPATH
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(PROJECT_WORKFLOW_YAML, encoding="utf-8")

    runner = FakeRunner()
    runner.release.clear()
    manager, calls = _manager(tmp_path, runner)
    context = _context(adapter={"runtime": "workflow", "scope": "project", "model": "openai/gpt-5"})

    result = manager.start_workflow(context, str(project_dir), "udelej X")
    assert result["workflow_file"] == PROJECT_WORKFLOW_FILE_RELPATH
    assert result["steps"] == ["Run agent"]

    # run soubory jdou mimo projekt do <project_run_root>/<run_id>/<attempt>/
    run_dir = manager.settings.project_run_root / context.run_id / result["attempt"]
    assert (run_dir / "prompt.md").read_text(encoding="utf-8") == "udelej X"
    assert json.loads((run_dir / "context.json").read_text(encoding="utf-8"))["task_id"] == "task-77"
    assert not (project_dir / ".agentis" / "runs").exists()

    # outputs vzniknou až během běhu — agent je píše do run dir
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "final-comment.md").write_text("Hotovo bez gitu.", encoding="utf-8")
    (outputs_dir / "session-id").write_text("ses_77\n", encoding="utf-8")

    runner.release.set()
    _wait_done(manager, context.task_id)

    comment_calls = [params for method, params in calls if method == "task.add_agent_comment"]
    assert len(comment_calls) == 1
    assert comment_calls[0]["body"] == "Hotovo bez gitu."
    assert comment_calls[0]["status"] == 5
    assert any(method == "run.store_session_id" for method, _ in calls)

    # Job dostane cesty do run dir, workdir zůstává projektový adresář
    assert len(runner.applied) == 1
    env = {item["name"]: item["value"] for item in runner.applied[0]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["AGENTIS_RUN_DIR"] == str(run_dir)
    assert env["AGENTIS_PROMPT_FILE"] == str(run_dir / "prompt.md")
    assert env["RUN_DIR"] == str(run_dir)
    assert env["WORKDIR"] == str(project_dir)


def test_project_scope_without_project_yaml_fails_with_clear_error(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    manager, _calls = _manager(tmp_path, FakeRunner())
    context = _context(adapter={"runtime": "workflow", "scope": "project"})

    with pytest.raises(FileNotFoundError, match=r"project\.yaml"):
        manager.start_workflow(context, str(project_dir), "udelej X")
    assert not (manager.settings.project_run_root / context.run_id).exists()


def test_failed_step_stops_workflow_and_reports_log_tail(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    _write_workflow(worktree)
    runner = FakeRunner()
    runner.results = ["failed"]
    manager, calls = _manager(tmp_path, runner)
    context = _context()

    manager.start_workflow(context, str(worktree), "udelej X")
    _wait_done(manager, context.task_id)

    assert len(runner.applied) == 1  # druhý krok se už nespustil
    failed_events = [
        params
        for method, params in calls
        if method == "run.adapter_event" and params["kind"] == "workflow_step" and params["status"] == "failed"
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["data"]["log_tail"] == "boom log"
    assert not any(method == "task.add_agent_comment" for method, _ in calls)
    assert manager._runs[context.task_id].status == "failed"


def test_abort_deletes_jobs_by_labels_without_session_id(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager, _calls = _manager(tmp_path, runner)
    context = _context(session_id=None)

    result = manager.abort(context)

    assert result["action"] == "abort"
    namespace, labels = runner.deleted[0]
    assert labels == {"agentis.task_id": "task-77", "agentis.run_id": "run-12345678"}
    assert namespace


# ---------------------------------------------------------------------------
# JSON-RPC integration (runtime=workflow)
# ---------------------------------------------------------------------------


class FakeWorkflowAdapter:
    def __init__(self, worktree: Path) -> None:
        self._worktree = worktree

    def create_worktree(self) -> dict[str, Any]:
        return {"action": "create_worktree", "working_dir": str(self._worktree)}

    def _workspace_path(self) -> Path:
        return self._worktree


def _service(tmp_path: Path, runner: FakeRunner) -> tuple[AgentJsonRpcService, WorkflowManager, list]:
    settings = _settings(tmp_path)
    worktree = tmp_path / "wt"
    manager, calls = _manager(tmp_path, runner)
    service = AgentJsonRpcService(
        settings=settings,
        adapter_factory=lambda context: FakeWorkflowAdapter(worktree),
        workflow_manager=manager,
    )
    return service, manager, calls


def test_jsonrpc_start_with_workflow_runtime_is_nonblocking(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    _write_workflow(worktree)
    runner = FakeRunner()
    runner.release.clear()
    service, manager, _calls = _service(tmp_path, runner)

    params = StartParams(context=_context(user_prompt="udelej X"))
    result = service.start(params)

    steps = result["adapter"]["steps"]
    assert steps[-1]["action"] == "workflow_start"
    assert result["run"]["status"] == "started"

    # druhý start na stejný task → 409 busy
    with pytest.raises(AgentJsonRpcException) as excinfo:
        service.start(StartParams(context=_context(user_prompt="znovu")))
    assert excinfo.value.code == 409

    runner.release.set()
    _wait_done(manager, "task-77")


def test_jsonrpc_add_message_reruns_ci_workflow_with_resume_session(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    _write_workflow(worktree)
    runner = FakeRunner()
    service, manager, _calls = _service(tmp_path, runner)
    context = _context(session_id="ses_42")

    result = service.add_message(AddMessageParams(run_id=context.run_id, context=context, message="oprav to"))

    steps = result["adapter"]["steps"]
    assert [step["action"] for step in steps] == ["create_worktree", "workflow_start"]
    _wait_done(manager, context.task_id)

    # feedback zpráva je prompt nového běhu, worktree se znovu použil
    run = manager._runs[context.task_id]
    assert run.prompt_file == worktree / ".agentis" / "runs" / run.attempt_id / "prompt.md"
    assert run.prompt_file.read_text(encoding="utf-8") == "oprav to"

    # session id z předchozího běhu jde do Jobu, aby agent mohl navázat (--resume)
    for manifest in runner.applied:
        env = {item["name"]: item["value"] for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["AGENTIS_SESSION_ID"] == "ses_42"


def test_jsonrpc_add_message_project_scope_skips_worktree_and_resumes_session(tmp_path: Path) -> None:
    project_dir = tmp_path / "wt"  # _service vrací FakeWorkflowAdapter s workspace tmp_path/"wt"
    workflow_path = project_dir / PROJECT_WORKFLOW_FILE_RELPATH
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(PROJECT_WORKFLOW_YAML, encoding="utf-8")
    runner = FakeRunner()
    service, manager, _calls = _service(tmp_path, runner)
    context = _context(
        session_id="ses_42",
        adapter={"runtime": "workflow", "scope": "project", "model": "openai/gpt-5"},
    )

    result = service.add_message(AddMessageParams(run_id=context.run_id, context=context, message="oprav to"))

    # žádný create_worktree krok, rovnou workflow_start
    steps = result["adapter"]["steps"]
    assert [step["action"] for step in steps] == ["workflow_start"]
    _wait_done(manager, context.task_id)

    run = manager._runs[context.task_id]
    assert run.prompt_file == manager.settings.project_run_root / context.run_id / run.attempt_id / "prompt.md"
    assert run.prompt_file.read_text(encoding="utf-8") == "oprav to"
    assert not (project_dir / ".agentis" / "runs").exists()

    assert len(runner.applied) == 1
    env = {item["name"]: item["value"] for item in runner.applied[0]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["AGENTIS_SESSION_ID"] == "ses_42"
    assert env["WORKDIR"] == str(project_dir)


def test_jsonrpc_question_is_unsupported_in_workflow_mode(tmp_path: Path) -> None:
    service, _manager, _calls = _service(tmp_path, FakeRunner())
    params = QuestionParams(
        run_id="run-12345678",
        context=_context(),
        request_id="q1",
        answers=[["ano"]],
    )
    with pytest.raises(AgentJsonRpcException) as excinfo:
        service.question(params)
    assert excinfo.value.code == 400


def test_jsonrpc_abort_in_workflow_mode_works_without_session_id(tmp_path: Path) -> None:
    runner = FakeRunner()
    service, _manager, _calls = _service(tmp_path, runner)
    result = service.abort(AbortParams(context=_context(session_id=None)))
    assert result["adapter"]["steps"][0]["action"] == "abort"
    assert runner.deleted
