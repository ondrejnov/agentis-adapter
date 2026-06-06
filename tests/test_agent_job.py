import json
import subprocess
from typing import Any

import pytest

from common.kubernetes.agent_job import (
    AGENT_JOB_NAME,
    AgentJobRunner,
    build_agent_job_manifest,
    resolve_run_manifest_path,
)

RUN_YAML = """
apiVersion: batch/v1
kind: Job
metadata:
  name: agent-run
  namespace: "[%NAMESPACE%]"
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: agent
          image: registry.test/opencode:1.2
          workingDir: "[%WORKDIR%]"
          command: ["/bin/bash", "-lc", "placeholder"]
          env:
            - name: AGENTIS_URL
              value: "[%AGENTIS_URL%]"
"""


def _pod_list(state: dict[str, Any]) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "agent-run-abc"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"name": "agent", "state": state}],
                    },
                }
            ]
        }
    )


def test_build_agent_job_manifest_injects_command_and_substitutes():
    manifest = build_agent_job_manifest(
        run_manifest_text=RUN_YAML,
        namespace="task-7-demo",
        workspace_path="/var/www/worktrees/task-7",
        agentis_url="http://adapter:8000",
        command_script="cd /work && exec opencode run --file /p",
    )

    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["name"] == AGENT_JOB_NAME
    assert manifest["metadata"]["namespace"] == "task-7-demo"
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["workingDir"] == "/var/www/worktrees/task-7"
    assert container["command"] == ["/bin/bash", "-lc", "cd /work && exec opencode run --file /p"]
    assert {"name": "AGENTIS_URL", "value": "http://adapter:8000"} in container["env"]


def test_build_agent_job_manifest_rejects_non_job():
    with pytest.raises(ValueError):
        build_agent_job_manifest(
            run_manifest_text="kind: Deployment\nmetadata: {}\n",
            namespace="ns",
            workspace_path="/w",
            agentis_url=None,
            command_script="echo hi",
        )


def test_resolve_run_manifest_path_prefers_workspace(tmp_path):
    (tmp_path / ".agentis").mkdir()
    manifest = tmp_path / ".agentis" / "run.yaml"
    manifest.write_text(RUN_YAML, encoding="utf-8")

    assert resolve_run_manifest_path(tmp_path, "/does/not/exist") == manifest


def test_resolve_run_manifest_path_falls_back_to_bundled_default():
    # No project ships run.yaml in these roots → bundled adapter default is used.
    resolved = resolve_run_manifest_path("/does/not/exist")
    assert resolved is not None
    assert resolved.name == "run.yaml"


def test_agent_job_runner_apply_and_logs(monkeypatch, tmp_path):
    manifest_path = tmp_path / "run.yaml"
    manifest_path.write_text(RUN_YAML, encoding="utf-8")
    runner = AgentJobRunner(
        kubectl="kubectl",
        namespace="task-7-demo",
        run_manifest_path=str(manifest_path),
        workspace_path="/var/www/worktrees/task-7",
        agentis_url=None,
    )

    calls: list[list[str]] = []

    def fake_run(args, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        stdout = ""
        if "get" in args and "pods" in args:
            stdout = _pod_list({"running": {"startedAt": "2026-06-06T12:00:00Z"}})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner.apply("cd /w && exec opencode run")
    pod = runner.wait_for_pod(timeout=1.0, interval=0.0)

    assert pod == "pod/agent-run-abc"
    assert runner.logs_argv(pod) == ["kubectl", "-n", "task-7-demo", "logs", "-f", "pod/agent-run-abc"]

    # The applied manifest carries the injected command.
    apply_call = next(call for call in calls if "apply" in call)
    assert apply_call[:2] == ["kubectl", "-n"]
    # delete of the stale job happens before apply
    assert any("delete" in call and "job" in call for call in calls)


def test_agent_job_runner_waits_until_pod_is_loggable(monkeypatch, tmp_path):
    manifest_path = tmp_path / "run.yaml"
    manifest_path.write_text(RUN_YAML, encoding="utf-8")
    runner = AgentJobRunner(
        kubectl="kubectl",
        namespace="task-7-demo",
        run_manifest_path=str(manifest_path),
        workspace_path="/var/www/worktrees/task-7",
        agentis_url=None,
    )

    pod_responses = [
        _pod_list({"waiting": {"reason": "ContainerCreating"}}),
        _pod_list({"running": {"startedAt": "2026-06-06T12:00:01Z"}}),
    ]
    calls: list[list[str]] = []

    def fake_run(args, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        stdout = pod_responses.pop(0)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    pod = runner.wait_for_pod(timeout=1.0, interval=0.0)

    assert pod == "pod/agent-run-abc"
    assert len(calls) == 2
