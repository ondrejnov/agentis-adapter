"""Kubernetes Job runtime pro workflow režim.

Generuje `batch/v1 Job` manifesty pro jednotlivé workflow kroky a obsluhuje je
přes `kubectl` subprocess (apply / wait / logs / delete). Žádný Python
Kubernetes client se nepoužívá.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import unicodedata
from typing import Any

import yaml

from common.config import Settings
from common.workflow.schema import WorkflowFile, WorkflowStep

WORKFLOW_LABEL = "agentis.workflow"

_KUBECTL_TIMEOUT_SEC = 60.0


def safe_step_name(name: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    sanitized = re.sub(r"[^a-z0-9-]+", "-", ascii_value.lower().strip())
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-")
    return sanitized[:20].strip("-") or "step"


def job_name(run_id: str, attempt_id: str, step_index: int, step_name: str) -> str:
    short_run = re.sub(r"[^a-z0-9]", "", run_id.lower())[:8] or "run"
    name = f"wf-{short_run}-{attempt_id}-{step_index}-{safe_step_name(step_name)}"
    return name[:63].strip("-")


def build_bash_wrapper(env_files: list[str], script: str, *, workdir_env: str = "WORKDIR") -> str:
    lines = ["set -euo pipefail"]
    for env_file in env_files:
        lines.extend(["set -a", f". {env_file}", "set +a"])
    lines.append(f'cd "${workdir_env}"')
    lines.append(script)
    return "\n".join(lines)


def job_labels(*, task_id: str, run_id: str, attempt_id: str, step_index: int, step_name: str) -> dict[str, str]:
    return {
        WORKFLOW_LABEL: "true",
        "agentis.task_id": safe_step_name(task_id) or "task",
        "agentis.run_id": re.sub(r"[^a-z0-9-]", "-", run_id.lower())[:63].strip("-") or "run",
        "agentis.attempt": attempt_id,
        "agentis.step_index": str(step_index),
        "agentis.step": safe_step_name(step_name),
    }


def build_job_manifest(
    workflow: WorkflowFile,
    step: WorkflowStep,
    *,
    namespace: str,
    name: str,
    labels: dict[str, str],
    env: dict[str, str],
) -> dict[str, Any]:
    spec = workflow.workflow
    image = step.image or spec.image
    working_dir = step.workingDir or spec.workingDir
    timeout = step.timeoutSeconds if step.timeoutSeconds is not None else spec.timeoutSeconds
    ttl = step.ttlSecondsAfterFinished if step.ttlSecondsAfterFinished is not None else spec.ttlSecondsAfterFinished

    merged_env = {**spec.env, **env, **step.env}
    container: dict[str, Any] = {
        "name": "step",
        "image": image,
        "command": ["/bin/bash", "-lc", build_bash_wrapper(spec.envFiles, step.run)],
        "env": [{"name": key, "value": value} for key, value in merged_env.items()],
    }
    if working_dir:
        container["workingDir"] = working_dir
    if spec.volumeMounts:
        container["volumeMounts"] = spec.volumeMounts
    if step.resources:
        container["resources"] = step.resources

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "containers": [container],
    }
    if workflow.volumes:
        pod_spec["volumes"] = workflow.volumes
    if spec.imagePullSecrets:
        pod_spec["imagePullSecrets"] = spec.imagePullSecrets

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": timeout,
            "ttlSecondsAfterFinished": ttl,
            "template": {
                "metadata": {"labels": labels},
                "spec": pod_spec,
            },
        },
    }


class KubectlJobRunner:
    """Tenký wrapper nad `kubectl` pro životní cyklus workflow Jobů."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _run(self, *args: str, stdin: str | None = None, timeout: float = _KUBECTL_TIMEOUT_SEC) -> str:
        completed = subprocess.run(
            [self.settings.kubectl_command, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown kubectl error"
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {stderr}")
        return completed.stdout

    def ensure_namespace(self, namespace: str) -> None:
        manifest = yaml.safe_dump({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})
        self._run("apply", "-f", "-", stdin=manifest)

    def apply_job(self, manifest: dict[str, Any]) -> None:
        self._run("apply", "-f", "-", stdin=yaml.safe_dump(manifest))

    def job_status(self, namespace: str, name: str) -> dict[str, Any]:
        output = self._run("get", "job", name, "-n", namespace, "-o", "json")
        status = json.loads(output).get("status")
        return status if isinstance(status, dict) else {}

    def wait_for_job(
        self,
        namespace: str,
        name: str,
        *,
        timeout: float,
        interval: float = 1.0,
        abort_event: threading.Event | None = None,
    ) -> str:
        """Sleduje Job do dokončení; vrací `succeeded` / `failed` / `timeout` / `aborted`."""

        deadline = time.monotonic() + timeout
        while True:
            if abort_event is not None and abort_event.is_set():
                return "aborted"
            try:
                status = self.job_status(namespace, name)
            except RuntimeError:
                status = {}
            if int(status.get("succeeded") or 0) > 0:
                return "succeeded"
            if int(status.get("failed") or 0) > 0:
                return "failed"
            for condition in status.get("conditions") or []:
                if condition.get("type") in {"Failed", "Complete"} and condition.get("status") == "True":
                    return "succeeded" if condition["type"] == "Complete" else "failed"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(interval)

    def job_log_tail(self, namespace: str, name: str, *, lines: int = 50) -> str:
        try:
            return self._run("logs", f"job/{name}", "-n", namespace, "--tail", str(lines)).strip()
        except RuntimeError as exc:
            return f"(log unavailable: {exc})"

    def delete_jobs_by_labels(self, namespace: str, labels: dict[str, str]) -> str:
        selector = ",".join(f"{key}={value}" for key, value in labels.items())
        return self._run("delete", "job", "-n", namespace, "-l", selector, "--ignore-not-found").strip()

    def has_active_jobs(self, namespace: str, task_label: str) -> bool:
        try:
            output = self._run(
                "get",
                "job",
                "-n",
                namespace,
                "-l",
                f"{WORKFLOW_LABEL}=true,agentis.task_id={task_label}",
                "-o",
                "json",
            )
        except RuntimeError:
            return False
        items = json.loads(output).get("items") or []
        for item in items:
            status = item.get("status") or {}
            if int(status.get("active") or 0) > 0:
                return True
        return False


__all__ = [
    "WORKFLOW_LABEL",
    "KubectlJobRunner",
    "build_bash_wrapper",
    "build_job_manifest",
    "job_labels",
    "job_name",
    "safe_step_name",
]
