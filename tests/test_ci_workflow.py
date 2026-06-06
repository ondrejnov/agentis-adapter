import subprocess
from pathlib import Path
from typing import Any

import pytest

from common.config import Settings
from common.kubernetes.ci_workflow import (
    CiAttachment,
    CiStep,
    CiWorkflow,
    CiWorkflowError,
    build_step_job_manifest,
    load_ci_workflow,
    load_finish_workflow,
    step_job_name,
)
from common.kubernetes.runtime import KubernetesRuntime
from common.models import AdapterOptionsPayload, AgentExecutionContextPayload

WORKFLOW_YAML = """
version: 1
volumes:
  - name: www
    hostPath:
      path: /var/www
  - name: npm-cache
    hostPath:
      path: /root/.npm
      type: DirectoryOrCreate
  - name: gitconfig
    hostPath:
      path: /root/.gitconfig
      type: FileOrCreate
  - name: gh-config
    hostPath:
      path: /root/.config/gh
      type: DirectoryOrCreate
setup:
  image: registry.test/opencode:1.2
  workdir: "[%WORKDIR%]"
  env:
    HOME: /root
    MAIN_DIR: "[%MAIN_DIR%]"
  volumeMounts:
    - name: www
      mountPath: /var/www
    - name: npm-cache
      mountPath: /root/.npm
  steps:
    - name: Create virtualenv
      run: python3.13 -m venv .venv
    - name: Install dependencies
      run: |
        poetry lock --no-update
        poetry install
"""


def test_load_ci_workflow_parses_steps(tmp_path):
    path = tmp_path / "ci.yaml"
    path.write_text(WORKFLOW_YAML, encoding="utf-8")

    workflow = load_ci_workflow(path)

    assert workflow is not None
    assert workflow.image == "registry.test/opencode:1.2"
    assert workflow.workdir == "[%WORKDIR%]"
    assert workflow.env == {"HOME": "/root", "MAIN_DIR": "[%MAIN_DIR%]"}
    assert workflow.volume_mounts == (
        {"name": "www", "mountPath": "/var/www"},
        {"name": "npm-cache", "mountPath": "/root/.npm"},
    )
    assert workflow.volumes == (
        {"name": "www", "hostPath": {"path": "/var/www"}},
        {"name": "npm-cache", "hostPath": {"path": "/root/.npm", "type": "DirectoryOrCreate"}},
        {"name": "gitconfig", "hostPath": {"path": "/root/.gitconfig", "type": "FileOrCreate"}},
        {"name": "gh-config", "hostPath": {"path": "/root/.config/gh", "type": "DirectoryOrCreate"}},
    )
    assert [(step.id, step.name) for step in workflow.steps] == [
        ("1-create-virtualenv", "Create virtualenv"),
        ("2-install-dependencies", "Install dependencies"),
    ]
    assert workflow.steps[1].run.strip().splitlines() == ["poetry lock --no-update", "poetry install"]


def test_load_ci_workflow_missing_file_returns_none(tmp_path):
    assert load_ci_workflow(tmp_path / "absent.yaml") is None


@pytest.mark.parametrize(
    "body",
    [
        "version: 1\n",  # no setup
        "setup:\n  image: x\n",  # no steps
        "setup:\n  steps:\n    - name: x\n      run: echo hi\n",  # no image
        "setup:\n  image: x\n  steps:\n    - name: x\n",  # step without run
        "volumes: nope\nsetup:\n  image: x\n  steps:\n    - run: echo hi\n",
        "setup:\n  image: x\n  volumeMounts: nope\n  steps:\n    - run: echo hi\n",
        "setup:\n  image: x\n  volumes:\n    - nope\n  steps:\n    - run: echo hi\n",
    ],
)
def test_load_ci_workflow_invalid_raises(tmp_path, body):
    path = tmp_path / "ci.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(CiWorkflowError):
        load_ci_workflow(path)


def test_build_step_job_manifest_substitutes_and_wraps_command():
    step = CiStep(id="1-create-virtualenv", name="Create virtualenv", run="python3.13 -m venv .venv")
    workflow = load_ci_workflow_from_text(WORKFLOW_YAML)

    manifest = build_step_job_manifest(
        workflow=workflow,
        step=step,
        namespace="task-7-demo",
        workspace_path="/var/www/worktrees/task-7",
        main_dir="/var/www/repo",
    )

    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["name"] == "ci-1-create-virtualenv"
    assert manifest["metadata"]["namespace"] == "task-7-demo"
    assert manifest["spec"]["backoffLimit"] == 0

    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    container = pod_spec["containers"][0]
    assert container["image"] == "registry.test/opencode:1.2"
    assert container["workingDir"] == "/var/www/worktrees/task-7"
    assert {"name": "MAIN_DIR", "value": "/var/www/repo"} in container["env"]
    assert container["command"][:3] == ["/bin/bash", "-eo", "pipefail"]
    script = container["command"][-1]
    assert "=== step: Create virtualenv ===" in script
    assert "python3.13 -m venv .venv" in script
    # workspace persists across step pods via the /var/www hostPath
    assert {"name": "www", "mountPath": "/var/www"} in container["volumeMounts"]


FINISH_WORKFLOW_YAML = (
    WORKFLOW_YAML
    + """
finish:
  image: registry.test/opencode:1.2
  workdir: "[%WORKDIR%]"
  env:
    TASK_NUMBER: "[%TASK_NUMBER%]"
    BRANCH: "[%BRANCH%]"
  volumeMounts:
    - name: www
      mountPath: /var/www
    - name: gitconfig
      mountPath: /root/.gitconfig
      readOnly: true
    - name: gh-config
      mountPath: /root/.config/gh
      readOnly: true
  steps:
    - name: Commit changes
      run: git add -A
    - name: Create pull request
      run: gh pr create
      attachments:
        - label: Pull Request
          type: url
          valueFrom: .agentis/outputs/pull-request-url
"""
)


def test_load_finish_workflow_parses_finish_phase(tmp_path):
    path = tmp_path / "ci.yaml"
    path.write_text(FINISH_WORKFLOW_YAML, encoding="utf-8")

    workflow = load_finish_workflow(path)

    assert workflow is not None
    assert workflow.env == {"TASK_NUMBER": "[%TASK_NUMBER%]", "BRANCH": "[%BRANCH%]"}
    assert {volume["name"] for volume in workflow.volumes} == {"www", "npm-cache", "gitconfig", "gh-config"}
    assert [step.name for step in workflow.steps] == ["Commit changes", "Create pull request"]
    assert workflow.steps[1].attachments == (
        CiAttachment(label="Pull Request", type="url", value_from=".agentis/outputs/pull-request-url"),
    )


def test_load_finish_workflow_absent_returns_none(tmp_path):
    path = tmp_path / "ci.yaml"
    path.write_text(WORKFLOW_YAML, encoding="utf-8")
    assert load_finish_workflow(path) is None


def test_build_finish_step_manifest_adds_git_volumes_and_replacements():
    workflow = load_ci_workflow_from_text(FINISH_WORKFLOW_YAML)
    finish = load_finish_workflow_from_text(FINISH_WORKFLOW_YAML)
    step = finish.steps[0]

    manifest = build_step_job_manifest(
        workflow=finish,
        step=step,
        namespace="task-7-demo",
        workspace_path="/var/www/worktrees/task-7",
        extra_replacements={"[%TASK_NUMBER%]": "7", "[%BRANCH%]": "task-7"},
        job_prefix="finish",
        app_label="finish",
    )

    assert manifest["metadata"]["name"] == "finish-1-commit-changes"
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "TASK_NUMBER", "value": "7"} in container["env"]
    assert {"name": "BRANCH", "value": "task-7"} in container["env"]
    mount_names = {mount["name"] for mount in container["volumeMounts"]}
    assert {"gitconfig", "gh-config", "www"}.issubset(mount_names)
    assert workflow.image == "registry.test/opencode:1.2"


def load_finish_workflow_from_text(text: str) -> CiWorkflow:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        handle.write(text)
        tmp = Path(handle.name)
    workflow = load_finish_workflow(tmp)
    assert workflow is not None
    return workflow


def test_step_job_name_is_dns_safe_and_truncated():
    step = CiStep(id="x" * 80, name="x", run="echo hi")
    name = step_job_name(step)
    assert len(name) <= 63
    assert not name.endswith("-")


def load_ci_workflow_from_text(text: str) -> CiWorkflow:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        handle.write(text)
        tmp = Path(handle.name)
    workflow = load_ci_workflow(tmp)
    assert workflow is not None
    return workflow


def _make_runtime(workspace: Path) -> KubernetesRuntime:
    settings = Settings(
        host="127.0.0.1",
        port=8003,
        default_namespace="agentis",
        app_host=None,
        manifest_path=Path("/tmp/opencode.yaml"),
        worktree_root=Path("/var/www/worktrees"),
        public_base_url="http://adapter.internal:8003",
        agentis_endpoint=None,
        agentis_token=None,
    )
    context = AgentExecutionContextPayload(
        run_id="run-1",
        task_id="task-1",
        title="Task",
        project_slug="agentis",
        working_dir="/var/www/repo",
        namespace="task-7-demo",
        adapter=AdapterOptionsPayload(agent="build"),
    )
    return KubernetesRuntime(context, settings, workspace)


def test_ci_setup_steps_reads_workspace_workflow(tmp_path):
    (tmp_path / ".agentis").mkdir()
    (tmp_path / ".agentis" / "ci.yaml").write_text(WORKFLOW_YAML, encoding="utf-8")

    steps = _make_runtime(tmp_path).ci_setup_steps()

    assert [step.name for step in steps] == ["Create virtualenv", "Install dependencies"]


def test_ci_setup_steps_empty_without_workflow(tmp_path):
    assert _make_runtime(tmp_path).ci_setup_steps() == []


def test_run_ci_step_applies_job_and_waits_for_completion(tmp_path, monkeypatch):
    (tmp_path / ".agentis").mkdir()
    (tmp_path / ".agentis" / "ci.yaml").write_text(WORKFLOW_YAML, encoding="utf-8")
    runtime = _make_runtime(tmp_path)
    step = runtime.ci_setup_steps()[0]

    calls: list[list[str]] = []

    def fake_run(args, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        joined = " ".join(args)
        stdout = ""
        if "jsonpath={.status.succeeded}" in joined:
            stdout = "1"
        elif "logs" in args:
            stdout = "step log output"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runtime.run_ci_step(step, timeout=5.0, interval=0.0)

    assert result["action"] == "ci_setup"
    assert result["job"] == "ci-1-create-virtualenv"
    assert result["namespace"] == "task-7-demo"
    assert result["logs"] == "step log output"
    verbs = [args[1] for args in calls if len(args) > 1]
    assert "apply" in verbs  # namespace + job applied
    assert "get" in verbs  # polled for completion


def test_run_ci_step_raises_on_failed_job(tmp_path, monkeypatch):
    (tmp_path / ".agentis").mkdir()
    (tmp_path / ".agentis" / "ci.yaml").write_text(WORKFLOW_YAML, encoding="utf-8")
    runtime = _make_runtime(tmp_path)
    step = runtime.ci_setup_steps()[0]

    def fake_run(args, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        stdout = ""
        if "jsonpath={.status.failed}" in joined:
            stdout = "1"
        elif "logs" in args:
            stdout = "boom"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="boom"):
        runtime.run_ci_step(step, timeout=5.0, interval=0.0)


def test_run_finish_step_reads_attachment_outputs(tmp_path, monkeypatch):
    (tmp_path / ".agentis" / "outputs").mkdir(parents=True)
    (tmp_path / ".agentis" / "ci.yaml").write_text(FINISH_WORKFLOW_YAML, encoding="utf-8")
    (tmp_path / ".agentis" / "outputs" / "pull-request-url").write_text(
        "https://github.com/example/repo/pull/42/changes\n",
        encoding="utf-8",
    )
    runtime = _make_runtime(tmp_path)
    step = runtime.finish_steps()[1]

    def fake_run(args, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        stdout = ""
        if "jsonpath={.status.succeeded}" in joined:
            stdout = "1"
        elif "logs" in args:
            stdout = "step log output"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runtime.run_finish_step(step, timeout=5.0, interval=0.0)

    assert result["attachments"] == [
        {
            "label": "Pull Request",
            "value": "https://github.com/example/repo/pull/42/changes",
            "type": "url",
        }
    ]
