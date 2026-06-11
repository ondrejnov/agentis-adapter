"""Deklarativní workflow schema pro `.agentis/workflows/default.yaml`.

Workflow režim přesouvá projektově proměnlivou logiku z Python adapteru do
YAML souboru ve worktree. Tady žije jeho Pydantic schema, načítání přes PyYAML
a interpolace `[%NAME%]` tokenů. Soubor se načte a zmrazí jednou na začátku
workflow runu — pozdější změny ve worktree běžící workflow neovlivní.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WORKFLOW_DIR_RELPATH = ".agentis/workflows"

WORKFLOW_FILE_RELPATH = f"{WORKFLOW_DIR_RELPATH}/default.yaml"

#: Workflow pro scope=project: běží přímo v adresáři projektu, bez worktree a git operací.
PROJECT_WORKFLOW_FILE_RELPATH = f"{WORKFLOW_DIR_RELPATH}/project.yaml"


def workflow_file_relpath(name: str) -> str:
    """Relativní cesta k pojmenovanému workflow (`context.adapter.workflow`)."""

    return f"{WORKFLOW_DIR_RELPATH}/{name}.yaml"

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


class WorkflowFollowup(BaseModel):
    """Followup akce nabídnutá v completion komentáři po doběhnutí workflow.

    Nejsou to samostatné RPC metody — akce dispatchne `start` s názvem workflow
    v kontextu (`context.adapter.workflow`) a adapter spustí
    `.agentis/workflows/<workflow>.yaml`.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    prompt: str = ""
    workflow: str
    continue_previous_run: bool = False

    def to_action(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "prompt": self.prompt,
            "adapter_method": "start",
            "workflow": self.workflow,
            "continue_previous_run": self.continue_previous_run,
        }


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
    #: Po úspěšném doběhnutí workflow smaže celý namespace; platí jen pro executor
    #: `kubernetes` (lokální executor žádné namespacy nevytváří a flag ignoruje).
    deleteNamespace: bool = False
    workingDir: str | None = None
    timeoutSeconds: int = 14400
    ttlSecondsAfterFinished: int = 3600
    env: dict[str, str] = Field(default_factory=dict)
    envFiles: list[str] = Field(default_factory=list)
    volumeMounts: list[dict[str, Any]] = Field(default_factory=list)
    imagePullSecrets: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[WorkflowStep] = Field(min_length=1)
    #: Followup akce nabídnuté po doběhnutí workflow; bez sekce se žádné nenabízí.
    followups: list[WorkflowFollowup] = Field(default_factory=list)

    @field_validator("env", mode="before")
    @classmethod
    def coerce_env(cls, value: Any) -> Any:
        return _coerce_env(value) if isinstance(value, dict) else value


class WorkflowFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    #: Jméno rodičovského souboru v `.agentis/workflows/` (bez `.yaml`), ze kterého
    #: soubor dědí konfiguraci. Vyřeší ho `load_workflow_file()` před validací.
    extends: str | None = None
    workflow: WorkflowSpec
    volumes: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowExtendsError(ValueError):
    pass


#: Pole `workflow` spec, která se z rodiče NIKDY nedědí — potomek je musí definovat sám.
_NON_INHERITED_SPEC_FIELDS = ("steps", "followups")

#: Seznamová pole `workflow` spec slučovaná položkově (rodič + potomek, viz `_merge_list`).
_MERGED_SPEC_LIST_FIELDS = ("envFiles", "volumeMounts", "imagePullSecrets")


def _merge_list(parent: list[Any], child: list[Any]) -> list[Any]:
    """Sloučí seznamová pole rodiče a potomka: konkatenace s přepisem podle `name`.

    Položky-mapy se stejným `name` (volumes, volumeMounts, imagePullSecrets)
    potomek přepisuje na místě — konkatenace by vyrobila duplicitní jména
    v Job manifestu. Ostatní položky se přidávají na konec, přesné duplikáty
    (typicky stejný řádek v `envFiles`) se vynechají.
    """

    merged = list(parent)
    index_by_name = {
        item["name"]: index for index, item in enumerate(merged) if isinstance(item, dict) and "name" in item
    }
    for item in child:
        if isinstance(item, dict) and item.get("name") in index_by_name:
            merged[index_by_name[item["name"]]] = item
        elif item not in merged:
            merged.append(item)
    return merged


def _merge_workflow_raw(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Sloučí surové YAML mapy rodiče a potomka podle dědičné sémantiky.

    Skaláry přepisuje potomek, `env` se merguje po klíčích (potomek vyhrává),
    seznamy infrastruktury (`volumes`, `envFiles`, `volumeMounts`,
    `imagePullSecrets`) se slučují přes `_merge_list`, `steps` a `followups`
    se nedědí nikdy. Merge běží nad surovými daty před validací a interpolací,
    aby se defaulty schématu neprosadily místo hodnot rodiče.
    """

    merged = {key: value for key, value in parent.items() if key != "workflow"}
    merged.update({key: value for key, value in child.items() if key not in {"workflow", "extends"}})
    merged["volumes"] = _merge_list(parent.get("volumes") or [], child.get("volumes") or [])

    parent_spec = dict(parent.get("workflow") or {})
    child_spec = dict(child.get("workflow") or {})
    for field in _NON_INHERITED_SPEC_FIELDS:
        parent_spec.pop(field, None)
    spec = {**parent_spec, **child_spec}
    spec["env"] = {**(parent_spec.get("env") or {}), **(child_spec.get("env") or {})}
    for field in _MERGED_SPEC_LIST_FIELDS:
        spec[field] = _merge_list(parent_spec.get(field) or [], child_spec.get(field) or [])
    merged["workflow"] = spec
    return merged


def _read_workflow_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Workflow file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Workflow file {path} must contain a YAML mapping")
    return raw


def _resolve_extends(workflow_path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    """Vyřeší top-level `extends: <name>` — jedna úroveň dědičnosti, bez řetězení."""

    extends = raw.get("extends")
    if extends is None:
        return raw
    if not isinstance(extends, str) or not extends:
        raise WorkflowExtendsError(f"Workflow file {workflow_path}: 'extends' must be a workflow name string")
    parent_path = workflow_path.parent / f"{extends}.yaml"
    if parent_path.resolve() == workflow_path.resolve():
        raise WorkflowExtendsError(f"Workflow file {workflow_path} cannot extend itself")
    if not parent_path.is_file():
        raise FileNotFoundError(f"Workflow extends target not found: {parent_path} (extends: {extends})")
    parent_raw = _read_workflow_yaml(parent_path)
    if parent_raw.get("extends") is not None:
        raise WorkflowExtendsError(
            f"Workflow file {workflow_path}: chained 'extends' is not supported "
            f"({parent_path} itself declares 'extends')"
        )
    return _merge_workflow_raw(parent_raw, raw)


def load_workflow_file(path: str | Path, values: dict[str, str]) -> WorkflowFile:
    """Načte workflow YAML, vyřeší `extends`, interpoluje tokeny a zvaliduje schema."""

    workflow_path = Path(path)
    raw = _resolve_extends(workflow_path, _read_workflow_yaml(workflow_path))
    return WorkflowFile.model_validate(interpolate_tokens(raw, values))


def load_workflow_followups(path: str | Path) -> list[WorkflowFollowup]:
    """Best-effort načte jen `workflow.followups` sekci workflow souboru.

    Pro completion komentáře lokálních sessions, kde se workflow nespouští
    (a plná validace spec by vyžadovala interpolaci a K8s pole). Chybějící
    soubor nebo nevalidní obsah znamená žádné followup akce — dokončení runu
    nesmí spadnout na rozbité konfiguraci, ta se projeví až při startu workflow.
    """

    workflow_path = Path(path)
    if not workflow_path.is_file():
        return []
    try:
        raw = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        spec = raw.get("workflow") if isinstance(raw, dict) else None
        items = spec.get("followups") if isinstance(spec, dict) else None
        if not isinstance(items, list):
            return []
        return [WorkflowFollowup.model_validate(item) for item in items]
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[workflow] invalid followups in {workflow_path}: {exc!r}\n")
        return []


__all__ = [
    "WORKFLOW_DIR_RELPATH",
    "WORKFLOW_FILE_RELPATH",
    "PROJECT_WORKFLOW_FILE_RELPATH",
    "workflow_file_relpath",
    "WORKFLOW_EXECUTORS",
    "INTERPOLATION_ALLOWLIST",
    "WorkflowConditionError",
    "WorkflowExtendsError",
    "WorkflowFollowup",
    "WorkflowInterpolationError",
    "WorkflowOutput",
    "WorkflowStep",
    "WorkflowSpec",
    "WorkflowFile",
    "evaluate_condition",
    "interpolate_tokens",
    "load_workflow_file",
    "load_workflow_followups",
    "parse_condition",
]
