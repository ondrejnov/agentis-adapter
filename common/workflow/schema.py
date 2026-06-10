"""Deklarativní workflow schema pro `.agentis/workflows/ci.yaml`.

Workflow režim přesouvá projektově proměnlivou logiku z Python adapteru do
YAML souboru ve worktree. Tady žije jeho Pydantic schema, načítání přes PyYAML
a interpolace `[%NAME%]` tokenů. Soubor se načte a zmrazí jednou na začátku
workflow runu — pozdější změny ve worktree běžící workflow neovlivní.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WORKFLOW_FILE_RELPATH = ".agentis/workflows/ci.yaml"

#: Workflow pro scope=project: běží přímo v adresáři projektu, bez worktree a git operací.
PROJECT_WORKFLOW_FILE_RELPATH = ".agentis/workflows/project.yaml"

#: Tokeny povolené pro interpolaci ve string hodnotách YAML.
INTERPOLATION_ALLOWLIST = (
    "NAMESPACE",
    "WORKDIR",
    "RUN_DIR",
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


#: Jména workflow proměnných musí být env-safe — injektují se do prostředí dalších kroků.
_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Podmínka kroku: `VAR`, `!VAR`, `VAR == hodnota`, `VAR != 'hodnota'`.
_CONDITION_RE = re.compile(
    r"^\s*(?P<neg>!)?\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*(?P<op>==|!=)\s*(?P<value>'[^']*'|\"[^\"]*\"|[^\s'\"]+))?\s*$"
)

#: Hodnoty považované za nepravdivé v holém `VAR` / `!VAR` testu (case-insensitive).
_FALSY_VALUES = {"", "0", "false", "no"}


class WorkflowConditionError(ValueError):
    pass


def parse_condition(expression: str) -> re.Match[str]:
    """Zvaliduje syntaxi `if` podmínky a vrátí match s groupami neg/name/op/value."""

    match = _CONDITION_RE.match(expression)
    if match is None:
        raise WorkflowConditionError(
            f"Invalid workflow condition {expression!r}; expected VAR, !VAR, VAR == value or VAR != value"
        )
    if match.group("neg") and match.group("op"):
        raise WorkflowConditionError(f"Invalid workflow condition {expression!r}; cannot combine '!' with comparison")
    return match


def evaluate_condition(expression: str, variables: Mapping[str, str]) -> bool:
    """Vyhodnotí `if` podmínku kroku nad proměnnými z předchozích kroků.

    Neznámá proměnná se chová jako prázdný string, holý test bere
    ``""``/``0``/``false``/``no`` jako nepravdu.
    """

    match = parse_condition(expression)
    actual = (variables.get(match.group("name")) or "").strip()
    op = match.group("op")
    if op is None:
        truthy = actual.lower() not in _FALSY_VALUES
        return not truthy if match.group("neg") else truthy
    expected = match.group("value")
    if expected[0] in {"'", '"'}:
        expected = expected[1:-1]
    return actual == expected if op == "==" else actual != expected


class WorkflowOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["agent_comment", "session_id", "url", "text", "artifact", "var"]
    label: str | None = None
    bodyFrom: str | None = None
    valueFrom: str | None = None
    status: int | None = None
    path: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def validate_var_output(self) -> WorkflowOutput:
        if self.type == "var":
            if not self.name or not _VAR_NAME_RE.match(self.name):
                raise ValueError("var output requires an env-safe 'name' ([A-Za-z_][A-Za-z0-9_]*)")
            if not self.valueFrom:
                raise ValueError("var output requires 'valueFrom'")
        return self


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    run: str
    if_: str | None = Field(default=None, alias="if")
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

    @field_validator("if_")
    @classmethod
    def validate_condition_syntax(cls, value: str | None) -> str | None:
        if value is not None:
            parse_condition(value)
        return value


#: Podporované executory workflow kroků: Kubernetes Joby vs. lokální bash procesy.
WORKFLOW_EXECUTORS = ("kubernetes", "local")


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Kde kroky poběží; bez hodnoty platí default adapteru (`WORKFLOW_EXECUTOR`).
    executor: Literal["kubernetes", "local"] | None = None
    #: Container image; povinný jen pro executor `kubernetes` (validuje WorkflowManager).
    image: str | None = None
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
    "PROJECT_WORKFLOW_FILE_RELPATH",
    "WORKFLOW_EXECUTORS",
    "INTERPOLATION_ALLOWLIST",
    "WorkflowConditionError",
    "WorkflowInterpolationError",
    "WorkflowOutput",
    "WorkflowStep",
    "WorkflowSpec",
    "WorkflowFile",
    "evaluate_condition",
    "interpolate_tokens",
    "load_workflow_file",
    "parse_condition",
]
