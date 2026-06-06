"""Run the agent CLI as a one-shot Kubernetes ``Job`` instead of ``kubectl exec``.

In ``kubernetes`` mode the CLI adapters used to ``kubectl exec`` the agent CLI
into a long-running ``opencode`` Deployment. That Deployment is gone: the agent
command (``agentiscode`` / ``opencode run`` / ``claude --print``) now runs as its
own short-lived ``Job`` declared by ``.agentis/run.yaml`` and terminates when the
agent is done.

This module is the small synchronous collaborator that the async CLI clients use
to apply the Job, wait for its pod, stream its logs (``kubectl logs -f``) and
delete it on abort/cleanup. The Job's pod spec (image, env, volumes) is
declarative in ``run.yaml``; only the actual command is injected here.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

AGENT_JOB_NAME = "agent-run"
RUN_MANIFEST_NAME = "run.yaml"

# Bundled fallback used when the worktree / source repo does not ship its own
# ``.agentis/run.yaml``. The adapter always runs from a checkout that contains it.
DEFAULT_RUN_MANIFEST_PATH = Path(__file__).resolve().parents[2] / ".agentis" / RUN_MANIFEST_NAME


def _substitute(text: str, namespace: str, workspace_path: str, agentis_url: str | None) -> str:
    return (
        text.replace("[%NAMESPACE%]", namespace)
        .replace("[%WORKDIR%]", workspace_path)
        .replace("[%MAIN_DIR%]", workspace_path)
        .replace("[%AGENTIS_URL%]", agentis_url or "")
    )


def build_agent_job_manifest(
    *,
    run_manifest_text: str,
    namespace: str,
    workspace_path: str,
    agentis_url: str | None,
    command_script: str,
    job_name: str = AGENT_JOB_NAME,
) -> dict[str, Any]:
    """Render the agent ``Job`` manifest from ``run.yaml`` with the command injected."""
    parsed = _substitute(run_manifest_text, namespace, workspace_path, agentis_url)
    manifest = yaml.safe_load(parsed)
    if not isinstance(manifest, dict) or manifest.get("kind") != "Job":
        raise ValueError("run.yaml must contain a single Kubernetes Job manifest")

    manifest.setdefault("metadata", {})
    manifest["metadata"]["name"] = job_name
    manifest["metadata"]["namespace"] = namespace

    try:
        container = manifest["spec"]["template"]["spec"]["containers"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("run.yaml Job is missing spec.template.spec.containers[0]") from exc
    container["command"] = ["/bin/bash", "-lc", command_script]
    return manifest


def resolve_run_manifest_path(*roots: Path | str | None) -> Path | None:
    """Return the first existing ``.agentis/run.yaml`` among the roots.

    Falls back to the adapter's bundled :data:`DEFAULT_RUN_MANIFEST_PATH`.
    """
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / ".agentis" / RUN_MANIFEST_NAME
        if candidate.is_file():
            return candidate
    if DEFAULT_RUN_MANIFEST_PATH.is_file():
        return DEFAULT_RUN_MANIFEST_PATH
    return None


@dataclass
class AgentJobRunner:
    """Apply / observe / delete a single agent ``Job`` via ``kubectl``."""

    kubectl: str
    namespace: str
    run_manifest_path: str
    workspace_path: str
    agentis_url: str | None = None
    job_name: str = AGENT_JOB_NAME

    def _kubectl(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.kubectl, "-n", self.namespace, *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def ensure_namespace(self) -> None:
        manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": self.namespace}}
        result = subprocess.run(
            [self.kubectl, "apply", "-f", "-"],
            input=yaml.safe_dump(manifest, sort_keys=False),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"kubectl apply namespace failed: {result.stderr.strip()}")

    def apply(self, command_script: str) -> None:
        manifest = build_agent_job_manifest(
            run_manifest_text=Path(self.run_manifest_path).read_text(encoding="utf-8"),
            namespace=self.namespace,
            workspace_path=self.workspace_path,
            agentis_url=self.agentis_url,
            command_script=command_script,
            job_name=self.job_name,
        )
        # Drop a stale job from a previous run so the manifest applies cleanly.
        self.delete(wait=True)
        result = self._kubectl("apply", "-f", "-", stdin=yaml.safe_dump(manifest, sort_keys=False))
        if result.returncode != 0:
            raise RuntimeError(f"kubectl apply agent job failed: {result.stderr.strip()}")

    def wait_for_pod(self, *, timeout: float = 120.0, interval: float = 1.0) -> str:
        """Block until the Job has a pod and return its ``pod/<name>`` reference."""
        deadline = time.monotonic() + timeout
        while True:
            result = self._kubectl(
                "get", "pods", "-l", f"job-name={self.job_name}", "-o", "name"
            )
            pod = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
            if pod:
                return pod
            if time.monotonic() >= deadline:
                raise TimeoutError(f"agent job {self.job_name} did not create a pod within {timeout}s")
            time.sleep(interval)

    def logs_argv(self, pod: str) -> list[str]:
        return [self.kubectl, "-n", self.namespace, "logs", "-f", pod]

    def delete(self, *, wait: bool = False) -> None:
        self._kubectl(
            "delete",
            "job",
            self.job_name,
            "--ignore-not-found=true",
            f"--wait={'true' if wait else 'false'}",
        )


__all__ = [
    "AGENT_JOB_NAME",
    "RUN_MANIFEST_NAME",
    "DEFAULT_RUN_MANIFEST_PATH",
    "AgentJobRunner",
    "build_agent_job_manifest",
    "resolve_run_manifest_path",
]
