from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HEADER_ENV_SANITIZE_RE = re.compile(r"[^A-Z0-9]+")


def task_header_env(headers: dict[str, Any] | None) -> dict[str, str]:
    """Převede hlavičky tasku na env proměnné ``TASK_HEADER_<KEY>``.

    Klíč se normalizuje na uppercase, nealfanumerické znaky na ``_``;
    hlavičky s prázdným klíčem po normalizaci se přeskočí. Skalární
    hodnoty se serializují přes ``str``, složené struktury jako JSON.
    """
    env: dict[str, str] = {}
    for key, value in (headers or {}).items():
        name = _HEADER_ENV_SANITIZE_RE.sub("_", str(key).upper()).strip("_")
        if not name:
            continue
        if value is None:
            serialized = ""
        elif isinstance(value, str):
            serialized = value
        elif isinstance(value, (dict, list)):
            serialized = json.dumps(value, ensure_ascii=False)
        else:
            serialized = str(value)
        env[f"TASK_HEADER_{name}"] = serialized
    return env


class TaskStatus:
    BACKLOG = 1
    TODO = 2
    IN_PROGRESS = 3
    IN_REVIEW = 4
    DONE = 5
    CANCELLED = 6
    BLOCKED = 7


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentAttachmentPayload(BaseModel):
    path: str
    filename: str | None = None
    mime: str | None = None
    content_base64: str | None = None


class ExpectedArtifactPayload(BaseModel):
    path: str
    name: str | None = None
    filename: str | None = None


class AdapterOptionsPayload(BaseModel):
    manifest: str | None = None
    branch: str | None = None
    scope: Literal["task", "worktree", "project"] = "task"
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    runtime: str | None = None
    task_status: int | None = None
    #: Název workflow souboru `.agentis/workflows/<workflow>.yaml`; vyplněný pro followup akce (merge, close, ...).
    workflow: str | None = None

    @field_validator("manifest")
    @classmethod
    def validate_manifest(cls, value: str | None) -> str | None:
        if value is None:
            return None

        manifest = value.strip()
        if not manifest:
            return None

        if manifest in {".", ".."} or PurePath(manifest).name != manifest or "\\" in manifest:
            raise ValueError("manifest must be a file name, not a path")

        return manifest

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str | None) -> str | None:
        if value is None:
            return None

        branch = value.strip()
        return branch or None

    @field_validator("workflow")
    @classmethod
    def validate_workflow(cls, value: str | None) -> str | None:
        if value is None:
            return None

        workflow = value.strip()
        if not workflow:
            return None

        if workflow in {".", ".."} or PurePath(workflow).name != workflow or "\\" in workflow:
            raise ValueError("workflow must be a workflow name, not a path")

        return workflow


class AgentCommentPayload(BaseModel):
    id: str | None = None
    author_type: str
    author_name: str | None = None
    body: str
    created: str | None = None
    updated: str | None = None
    run_id: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class AgentExecutionContextPayload(BaseModel):
    run_id: str
    task_id: str
    session_id: str | None = None
    title: str
    description: str = ""
    user_prompt: str | None = None
    task_status: int | None = None
    task_number: int | None = None
    task_priority: int | None = None
    parent_task_id: int | None = None
    headers: dict[str, Any] | None = None
    project_id: str | int | None = None
    project_title: str | None = None
    project_slug: str = "agentis"
    project_github_repo: str | None = None
    project_documentation: str | None = None
    ide: str | None = None
    base_branch: str = "master"
    agent_id: str | None = None
    agent_title: str | None = None
    agent_prompt: str | None = None
    comments: list[AgentCommentPayload] = Field(default_factory=list)
    attachments: list[AgentAttachmentPayload] = Field(default_factory=list)
    expected_artifacts: list[ExpectedArtifactPayload | str] | dict[str, Any] | None = None
    working_dir: str = "/var/www/agentis-general"
    namespace: str | None = None
    app_host: str | None = None
    adapter: AdapterOptionsPayload | None = None

    @model_validator(mode="before")
    @classmethod
    def apply_nullable_string_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        data = dict(value)
        for field_name in ("project_slug", "base_branch", "working_dir"):
            if data.get(field_name) is None:
                data[field_name] = cls.model_fields[field_name].default
        return data


def completion_task_status(context: AgentExecutionContextPayload | None) -> int:
    if context and context.adapter and context.adapter.task_status is not None:
        return context.adapter.task_status
    if context and context.adapter and context.adapter.scope == "project":
        return TaskStatus.DONE
    return TaskStatus.IN_REVIEW


class StartParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: AgentExecutionContextPayload
    fork_from_session_id: str | None = None


class AddMessageParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    context: AgentExecutionContextPayload
    message: str
    role: Literal["user", "agent", "system"] = "user"
    #: Přílohy feedback zprávy (backend je posílá mimo context.attachments).
    attachments: list[AgentAttachmentPayload] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_run_id_matches_context(self) -> AddMessageParams:
        if self.context.run_id != self.run_id:
            raise ValueError("run_id must match context.run_id")
        return self


class QuestionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    context: AgentExecutionContextPayload
    request_id: str
    answers: list[list[str]]

    @model_validator(mode="after")
    def validate_run_id_matches_context(self) -> QuestionParams:
        if self.context.run_id != self.run_id:
            raise ValueError("run_id must match context.run_id")
        return self


class ApproveParams(BaseModel):
    run_id: str
    approved: bool
    comment: str | None = None


class AbortParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: AgentExecutionContextPayload


class UndoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: AgentExecutionContextPayload


class RunEventPayload(BaseModel):
    kind: Literal["start", "message", "question", "approve", "proxy"]
    created_at: str = Field(default_factory=utc_now)
    payload: dict[str, Any]


class RunStatePayload(BaseModel):
    run_id: str
    status: Literal["started", "approved", "rejected", "failed"] = "started"
    context: AgentExecutionContextPayload
    opencode_session_id: str | None = None
    events: list[RunEventPayload] = Field(default_factory=list)

    def safe_dump(self) -> dict[str, Any]:
        return self.model_dump()
