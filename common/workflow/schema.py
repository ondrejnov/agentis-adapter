"""Deklarativní workflow schema pro `.agentis/workflows/ci.yaml`.

Workflow režim přesouvá projektově proměnlivou logiku z Python adapteru do
YAML souboru ve worktree. Tady žije jeho Pydantic schema, načítání přes PyYAML
a interpolace `[%NAME%]` tokenů. Soubor se načte a zmrazí jednou na začátku
workflow runu — pozdější změny ve worktree běžící workflow neovlivní.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

WORKFLOW_FILE_RELPATH = ".agentis/workflows/ci.yaml"

#: Tokeny povolené pro interpolaci ve string hodnotách YAML.
INTERPOLATION_ALLOWLIST = (
    "NAMESPACE",
    "WORKDIR",
    "MAIN_DIR",
    "RUN_ID",
    "TASK_ID",
    "TASK_NUMBER",
    "TASK_TITLE",
    "BRANCH",
    "BASE_BRANCH",
    "GITHUB_REPO",
)

_TOKEN_RE = re.compile(r"\[%([A-Z_]+)%\]")


class WorkflowInterpolationError(ValueError):
    pass


def interpolate_tokens(value: Any, values: dict[str, str]) -> Any:
    """Rekurzivně nahradí `[%NAME%]` tokeny ve string hodnotách struktury."""

    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in INTERPOLATION_ALLOWLIST:
                raise WorkflowInterpolationError(f"Unknown workflow token [%{name}%]")
            return values.get(name, "")

        return _TOKEN_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {key: interpolate_tokens(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate_tokens(item, values) for item in value]
    return value


def _coerce_env(value: dict[str, Any]) -> dict[str, str]:
    return {key: str(item) for key, item in value.items()}


class WorkflowOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["agent_comment", "session_id", "url", "text", "artifact"]
    label: str | None = None
    bodyFrom: str | None = None
    valueFrom: str | None = None
    status: int | None = None
    path: str | None = None
    name: str | None = None


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    run: str
    image: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    workingDir: str | None = None
    timeoutSeconds: int | None = None
    ttlSecondsAfterFinished: int | None = None
    resources: dict[str, Any] | None = None
    outputs: list[WorkflowOutput] = Field(default_factory=list)

    @field_validator("env", mode="before")
    @classmethod
    def coerce_env(cls, value: Any) -> Any:
        return _coerce_env(value) if isinstance(value, dict) else value


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    workingDir: str | None = None
    timeoutSeconds: int = 14400
    ttlSecondsAfterFinished: int = 3600
    env: dict[str, str] = Field(default_factory=dict)
    envFiles: list[str] = Field(default_factory=list)
    volumeMounts: list[dict[str, Any]] = Field(default_factory=list)
    imagePullSecrets: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[WorkflowStep] = Field(min_length=1)

    @field_validator("env", mode="before")
    @classmethod
    def coerce_env(cls, value: Any) -> Any:
        return _coerce_env(value) if isinstance(value, dict) else value


class WorkflowFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    workflow: WorkflowSpec
    volumes: list[dict[str, Any]] = Field(default_factory=list)


def load_workflow_file(path: str | Path, values: dict[str, str]) -> WorkflowFile:
    """Načte ci.yaml, interpoluje tokeny a zvaliduje schema."""

    workflow_path = Path(path)
    if not workflow_path.is_file():
        raise FileNotFoundError(f"Workflow file not found: {workflow_path}")

    raw = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Workflow file {workflow_path} must contain a YAML mapping")
    return WorkflowFile.model_validate(interpolate_tokens(raw, values))


__all__ = [
    "WORKFLOW_FILE_RELPATH",
    "INTERPOLATION_ALLOWLIST",
    "WorkflowInterpolationError",
    "WorkflowOutput",
    "WorkflowStep",
    "WorkflowSpec",
    "WorkflowFile",
    "interpolate_tokens",
    "load_workflow_file",
]
