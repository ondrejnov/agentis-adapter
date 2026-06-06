"""CI-style environment setup workflow.

The worktree environment is initialised by a declarative, GitHub-Actions-like
workflow file (``.agentis/ci.yaml``) instead of a monolithic ``setup.sh`` baked
into the Deployment as an init container. Each step is run as its own short-lived
Kubernetes ``Job`` against the shared workspace, so the runtime can report a
``started``/``success`` event per step and fail fast on the offending step.

This module is pure: it parses the workflow and renders the per-step Job manifest
dict. All ``kubectl`` execution lives in :mod:`common.kubernetes.runtime`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CI_WORKFLOW_PATH = ".agentis/ci.yaml"

# hostPath mounts shared by every setup step. The workspace lives under
# ``/var/www`` (a hostPath), so artefacts written by one step — most importantly
# ``.venv`` — persist into the next step's pod and into the running server.
_STEP_VOLUME_MOUNTS: tuple[dict[str, str], ...] = (
    {"name": "www", "mountPath": "/var/www"},
    {"name": "npm-cache", "mountPath": "/root/.npm"},
    {"name": "poetry-cache", "mountPath": "/root/.cache/pypoetry"},
    {"name": "gitnexus", "mountPath": "/root/.gitnexus"},
)

_STEP_VOLUMES: tuple[dict[str, Any], ...] = (
    {"name": "www", "hostPath": {"path": "/var/www"}},
    {"name": "npm-cache", "hostPath": {"path": "/root/.npm", "type": "DirectoryOrCreate"}},
    {"name": "poetry-cache", "hostPath": {"path": "/root/.cache/pypoetry", "type": "DirectoryOrCreate"}},
    {"name": "gitnexus", "hostPath": {"path": "/root/.gitnexus", "type": "DirectoryOrCreate"}},
)

# Extra mounts for the ``finish`` phase (commit / pull request): the steps need
# git identity and the GitHub CLI credentials to push and open PRs.
_FINISH_VOLUME_MOUNTS: tuple[dict[str, Any], ...] = (
    {"name": "gitconfig", "mountPath": "/root/.gitconfig", "readOnly": True},
    {"name": "gh-config", "mountPath": "/root/.config/gh", "readOnly": True},
)

_FINISH_VOLUMES: tuple[dict[str, Any], ...] = (
    {"name": "gitconfig", "hostPath": {"path": "/root/.gitconfig", "type": "FileOrCreate"}},
    {"name": "gh-config", "hostPath": {"path": "/root/.config/gh", "type": "DirectoryOrCreate"}},
)


class CiWorkflowError(ValueError):
    """Raised when the CI workflow file is missing required structure."""


@dataclass(frozen=True)
class CiStep:
    id: str
    name: str
    run: str


@dataclass(frozen=True)
class CiWorkflow:
    image: str
    workdir: str | None
    env: dict[str, str]
    steps: tuple[CiStep, ...]


def _slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def _read_document(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CiWorkflowError(f"CI workflow {path} is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise CiWorkflowError(f"CI workflow {path} must be a mapping")
    return document


def _parse_phase(document: dict[str, Any], path: Path, phase: str, *, required: bool) -> CiWorkflow | None:
    """Parse a workflow phase (``setup`` / ``finish``) into a :class:`CiWorkflow`."""
    section = document.get(phase)
    if section is None:
        if required:
            raise CiWorkflowError(f"CI workflow {path} must define a '{phase}' mapping")
        return None
    if not isinstance(section, dict):
        raise CiWorkflowError(f"CI workflow {path}: '{phase}' must be a mapping")

    image = section.get("image")
    if not isinstance(image, str) or not image.strip():
        raise CiWorkflowError(f"CI workflow {path}: {phase}.image must be a non-empty string")

    workdir = section.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        raise CiWorkflowError(f"CI workflow {path}: {phase}.workdir must be a string")

    env_raw = section.get("env") or {}
    if not isinstance(env_raw, dict):
        raise CiWorkflowError(f"CI workflow {path}: {phase}.env must be a mapping")
    env = {str(key): "" if value is None else str(value) for key, value in env_raw.items()}

    steps_raw = section.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise CiWorkflowError(f"CI workflow {path}: {phase}.steps must be a non-empty list")

    steps: list[CiStep] = []
    used_ids: set[str] = set()
    for index, item in enumerate(steps_raw, start=1):
        if not isinstance(item, dict):
            raise CiWorkflowError(f"CI workflow {path}: {phase} step {index} must be a mapping")
        run = item.get("run")
        if not isinstance(run, str) or not run.strip():
            raise CiWorkflowError(f"CI workflow {path}: {phase} step {index} must define a non-empty 'run'")
        name_raw = item.get("name")
        name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else f"step-{index}"

        step_id = f"{index}-{_slugify(name, f'step-{index}')}"
        while step_id in used_ids:
            step_id = f"{step_id}-x"
        used_ids.add(step_id)
        steps.append(CiStep(id=step_id, name=name, run=run))

    return CiWorkflow(
        image=image.strip(),
        workdir=workdir.strip() if workdir else None,
        env=env,
        steps=tuple(steps),
    )


def load_ci_workflow(path: Path) -> CiWorkflow | None:
    """Parse the ``setup`` phase of ``.agentis/ci.yaml``; ``None`` when absent."""
    document = _read_document(path)
    if document is None:
        return None
    return _parse_phase(document, path, "setup", required=True)


def load_finish_workflow(path: Path) -> CiWorkflow | None:
    """Parse the optional ``finish`` phase of ``.agentis/ci.yaml``.

    Returns ``None`` when the file or the ``finish`` section is absent.
    """
    document = _read_document(path)
    if document is None:
        return None
    return _parse_phase(document, path, "finish", required=False)


def _substitute(value: str, replacements: dict[str, str]) -> str:
    for placeholder, replacement in replacements.items():
        value = value.replace(placeholder, replacement)
    return value


def step_job_name(step: CiStep, *, prefix: str = "ci") -> str:
    return f"{prefix}-{step.id}"[:63].rstrip("-")


def build_step_job_manifest(
    *,
    workflow: CiWorkflow,
    step: CiStep,
    namespace: str,
    workspace_path: str,
    main_dir: str | None = None,
    agentis_url: str | None = None,
    extra_replacements: dict[str, str] | None = None,
    job_prefix: str = "ci",
    app_label: str = "ci-setup",
    extra_volume_mounts: tuple[dict[str, Any], ...] = (),
    extra_volumes: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Render the Kubernetes ``Job`` manifest for a single workflow step."""
    replacements = {
        "[%NAMESPACE%]": namespace,
        "[%WORKDIR%]": workspace_path,
        "[%MAIN_DIR%]": main_dir or workspace_path,
        "[%AGENTIS_URL%]": agentis_url or "",
        **(extra_replacements or {}),
    }
    working_dir = _substitute(workflow.workdir, replacements) if workflow.workdir else workspace_path
    env = [{"name": key, "value": _substitute(value, replacements)} for key, value in workflow.env.items()]

    script = f'echo "=== step: {step.name} ==="\n{step.run}'

    volume_mounts = [dict(mount) for mount in (*_STEP_VOLUME_MOUNTS, *extra_volume_mounts)]
    volumes = [dict(volume) for volume in (*_STEP_VOLUMES, *extra_volumes)]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": step_job_name(step, prefix=job_prefix),
            "namespace": namespace,
            "labels": {"app": app_label, "ci-step": step.id},
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {"app": app_label, "ci-step": step.id}},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "step",
                            "image": workflow.image,
                            "imagePullPolicy": "IfNotPresent",
                            "workingDir": working_dir,
                            "command": ["/bin/bash", "-eo", "pipefail", "-c", script],
                            "env": env,
                            "volumeMounts": volume_mounts,
                        }
                    ],
                    "volumes": volumes,
                    "imagePullSecrets": [{"name": "registry"}],
                },
            },
        },
    }


def namespace_manifest(namespace: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}


__all__ = [
    "CI_WORKFLOW_PATH",
    "CiStep",
    "CiWorkflow",
    "CiWorkflowError",
    "build_step_job_manifest",
    "load_ci_workflow",
    "load_finish_workflow",
    "namespace_manifest",
    "step_job_name",
    "_FINISH_VOLUME_MOUNTS",
    "_FINISH_VOLUMES",
]
