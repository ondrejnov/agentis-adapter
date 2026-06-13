from __future__ import annotations

from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from common.config import Settings
from common.models import (
    AddMessageParams,
    AgentExecutionContextPayload,
    AbortParams,
    RunEventPayload,
    RunStatePayload,
    StartParams,
    UndoParams,
)
from common.adapter_base import BaseAdapterService
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.attachments import build_attachments_block, materialize_attachments, next_attachment_index
from common.rpc.session_registry import SessionContextRegistry
from common.status import get_status_registry
from common.workflow.manager import WorkflowBusyError, WorkflowManager




class AgentJsonRpcException(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class AgentJsonRpcService:
    def __init__(
        self,
        settings: Settings,
        adapter_factory: Callable[[AgentExecutionContextPayload], BaseAdapterService],
        session_registry: SessionContextRegistry | None = None,
        workflow_manager: WorkflowManager | None = None,
    ):
        self.settings = settings
        self._adapter_factory = adapter_factory
        self.session_registry = session_registry or SessionContextRegistry()
        self._workflow_manager = workflow_manager

    @property
    def workflow_manager(self) -> WorkflowManager:
        if self._workflow_manager is None:
            self._workflow_manager = WorkflowManager(self.settings)
        return self._workflow_manager

    def active_count(self) -> int:
        """Běžící workflow runy; CLI session thready hlídá session manager na ``app.state``."""
        return self._workflow_manager.active_count() if self._workflow_manager is not None else 0

    def wait_idle(self, timeout: float | None = None) -> bool:
        if self._workflow_manager is None:
            return True
        return self._workflow_manager.wait_idle(timeout)

    @staticmethod
    def _workflow_prompt(context: AgentExecutionContextPayload) -> str:
        chunks: list[str] = []
        for text in (context.user_prompt, context.description):
            if isinstance(text, str) and text.strip() and (not chunks or chunks[-1] != text.strip()):
                chunks.append(text.strip())
        return "\n\n".join(chunks) or context.title

    @staticmethod
    def _prompt_with_attachments(
        prompt: str,
        context: AgentExecutionContextPayload,
        worktree: str,
        message_attachments: list[Any] | None,
    ) -> str:
        """Materializuje přílohy do worktree a doplní do promptu sekci ``<attachments>``.

        Při startu jde o task přílohy z kontextu (deterministické názvy od 1, stejně
        jako CLI runtime); u follow-up zprávy jen o přílohy zprávy s indexem
        navazujícím na už materializované soubory.
        """
        if message_attachments is None:
            materialized = materialize_attachments(worktree, context.attachments, task_id=context.task_id)
        else:
            materialized = materialize_attachments(
                worktree,
                message_attachments,
                task_id=context.task_id,
                start_index=next_attachment_index(worktree),
            )
        block = build_attachments_block(materialized)
        if not block:
            return prompt
        return f"{prompt}\n\n{block}" if prompt.strip() else block

    def _start_workflow_run(
        self,
        run: RunStatePayload,
        context: AgentExecutionContextPayload,
        prompt: str,
        message_attachments: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Spustí workflow na pozadí a rychle vrátí odpověď (bez session_id)."""
        adapter_steps: list[dict[str, Any]] = []
        try:
            adapter = self._adapter_factory(context)
            worktree: str | None = None
            if not BaseAdapterService.is_project_scope(context):
                worktree_step = self._run_adapter_step(
                    adapter,
                    kind="create_worktree",
                    success_message="Git worktree je připravený.",
                    callback=adapter.create_worktree,
                )
                adapter_steps.append(worktree_step)
                working_dir = worktree_step.get("working_dir")
                if isinstance(working_dir, str) and working_dir:
                    worktree = working_dir
            if worktree is None:
                worktree = str(adapter._workspace_path())
            prompt = self._prompt_with_attachments(prompt, context, worktree, message_attachments)
            workflow_step = self._run_adapter_step(
                adapter,
                kind="workflow_start",
                started_message="Spouštím workflow.",
                success_message="Workflow běží na pozadí.",
                callback=lambda: self.workflow_manager.start_workflow(context, worktree, prompt),
            )
            adapter_steps.append(workflow_step)
        except WorkflowBusyError as exc:
            run.status = "failed"
            get_status_registry().run_finished(run.run_id, "failed")
            raise AgentJsonRpcException(409, str(exc)) from exc
        except FileNotFoundError as exc:
            run.status = "failed"
            get_status_registry().run_finished(run.run_id, "failed")
            raise AgentJsonRpcException(400, str(exc)) from exc
        except Exception as exc:
            run.status = "failed"
            get_status_registry().run_finished(run.run_id, "failed")
            raise AgentJsonRpcException(500, f"Adapter error: {exc}") from exc
        return {
            "run": run.safe_dump(),
            "adapter": {
                "executed": True,
                "steps": adapter_steps,
            },
        }

    def start(self, params: StartParams) -> dict[str, Any]:
        context = params.context
        get_status_registry().run_received(context, kind="workflow", method="start")
        run = RunStatePayload(run_id=context.run_id, context=context)
        run.events.append(
            RunEventPayload(
                kind="start",
                payload={
                    "task_id": context.task_id,
                    "title": context.title,
                    "project_slug": context.project_slug,
                },
            )
        )
        return self._start_workflow_run(run, context, self._workflow_prompt(context))

    def _run_adapter_step(
        self,
        adapter: BaseAdapterService,
        *,
        kind: str,
        success_message: str,
        started_message: str | None = None,
        callback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        event_id = f"{kind}:{uuid4().hex}"
        if started_message:
            self._emit_adapter_event(
                adapter,
                kind=kind,
                status="started",
                event_id=event_id,
                message=started_message,
            )
        try:
            result = callback()
        except Exception as exc:
            self._emit_adapter_event(
                adapter,
                kind=kind,
                status="failed",
                event_id=event_id,
                message=str(exc),
                data={"error": str(exc)},
            )
            raise

        self._emit_adapter_event(
            adapter,
            kind=kind,
            status="success",
            event_id=event_id,
            message=success_message,
            data=result if isinstance(result, dict) else None,
        )
        return result

    @staticmethod
    def _emit_adapter_event(
        adapter: BaseAdapterService,
        *,
        kind: str,
        status: str,
        event_id: str,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        context = getattr(adapter, "context", None)
        run_id = getattr(context, "run_id", "") if context is not None else ""
        if run_id and message:
            get_status_registry().run_activity(run_id, message)

        reporter = getattr(adapter, "post_agentis_event", None)
        if not callable(reporter):
            return

        try:
            reporter(kind=kind, status=status, event_id=event_id, message=message, data=data)
        except Exception as exc:
            import sys

            sys.stderr.write(f"[adapter-event] failed kind={kind} status={status} event_id={event_id} error={exc!r}\n")
            sys.stderr.flush()
            return

    def add_message(self, params: AddMessageParams) -> dict[str, Any]:
        context = params.context
        get_status_registry().run_received(context, kind="workflow", method="add_message")
        run = RunStatePayload(run_id=context.run_id, context=context)
        run.events.append(
            RunEventPayload(
                kind="message",
                payload={
                    "task_id": context.task_id,
                    "title": context.title,
                    "project_slug": context.project_slug,
                },
            )
        )
        return self._start_workflow_run(run, context, params.message, message_attachments=params.attachments)

    def abort(self, params: AbortParams) -> dict[str, Any]:
        context = params.context
        get_status_registry().abort_received()
        run = RunStatePayload(run_id=context.run_id, context=context)
        try:
            step = self.workflow_manager.abort(context)
        except Exception as exc:
            run.status = "failed"
            raise AgentJsonRpcException(500, f"Adapter error: {exc}") from exc
        return {
            "run": run.safe_dump(),
            "adapter": {
                "executed": True,
                "steps": [step],
            },
        }

    def undo(self, params: UndoParams) -> dict[str, Any]:
        context = params.context
        # Per-session snapshot bere workflow runtime na startu runu (snapshot_sources_best_effort);
        # undo vrátí worktree do tohoto stavu přes adapter, stejně jako dřív.
        snapshot_key = self.workflow_manager.snapshot_key_for_task(context.task_id)
        if not snapshot_key:
            raise AgentJsonRpcException(400, f"No source snapshot is registered for task {context.task_id}")

        run = RunStatePayload(run_id=context.run_id, context=context)
        try:
            adapter = self._adapter_factory(context)
            step = self._run_adapter_step(
                adapter,
                kind="undo",
                started_message="Vracím pracovní strom do posledního snapshotu.",
                success_message="Pracovní strom byl vrácen do posledního snapshotu.",
                callback=lambda: adapter.restore_snapshot(snapshot_key),
            )
        except Exception as exc:
            run.status = "failed"
            raise AgentJsonRpcException(500, f"Adapter error: {exc}") from exc

        return {
            "run": run.safe_dump(),
            "adapter": {
                "executed": True,
                "steps": [step],
            },
        }

    def call_agentis(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.agentis_endpoint:
            raise AgentJsonRpcException(400, "agentis_endpoint is missing in adapter settings")

        endpoint = self.settings.agentis_endpoint

        try:
            with AgentisJsonRpcClient(endpoint=endpoint, token=self.settings.agentis_token) as client:
                result = client.call(method=method, params=params, request_id=f"agent-{method}-{uuid4().hex}")
        except AgentisJsonRpcError as exc:
            raise AgentJsonRpcException(
                502,
                f"Failed to forward `{method}` to Agentis: {exc}",
                exc.details,
            ) from exc

        return result if isinstance(result, dict) else {"ok": True, "result": result}

    @classmethod
    def _sanitize_for_log(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._sanitize_for_log(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._sanitize_for_log(item) for item in value]
        if isinstance(value, tuple):
            return [cls._sanitize_for_log(item) for item in value]
        if isinstance(value, BaseException):
            return str(value)
        return value


def validate_params(model: type[BaseModel], params: Any) -> BaseModel:
    try:
        return model.model_validate(params or {})
    except ValidationError as exc:
        raise AgentJsonRpcException(
            -32602,
            "Invalid params",
            AgentJsonRpcService._sanitize_for_log(exc.errors()),
        ) from exc
