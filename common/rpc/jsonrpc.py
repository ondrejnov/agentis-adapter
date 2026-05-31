from __future__ import annotations

from typing import Any, Callable, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from common.config import Settings
from common.models import (
    AddMessageParams,
    AgentExecutionContextPayload,
    AbortParams,
    ApproveParams,
    CloseParams,
    GitMergeParams,
    QuestionParams,
    RunEventPayload,
    RunStatePayload,
    StartParams,
    TaskStatus,
    UndoParams,
)
from common.adapter_base import BaseAdapterService
from common.kubernetes_runtime import KubernetesAdapterService
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.rpc.session_registry import SessionContextRegistry


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
        adapter_factory: Callable[[AgentExecutionContextPayload], BaseAdapterService] | None = None,
        session_registry: SessionContextRegistry | None = None,
    ):
        self.settings = settings
        self._adapter_factory = adapter_factory or (lambda context: KubernetesAdapterService(context, settings))
        self.session_registry = session_registry or SessionContextRegistry()

    def start(self, params: StartParams) -> dict[str, Any]:
        context = params.context
        print(context)
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

        adapter_steps: list[dict[str, Any]] = []
        try:
            adapter = self._adapter_factory(context)
            is_project_scope = BaseAdapterService.is_project_scope(context)
            if not is_project_scope:
                adapter_steps.append(
                    self._run_adapter_step(
                        adapter,
                        kind="create_worktree",
                        success_message="Git worktree je připravený.",
                        callback=adapter.create_worktree,
                    )
                )
            init_agentis = getattr(adapter, "init_agentis", None)
            if getattr(adapter, "requires_agentis_init", False) and callable(init_agentis):
                init_agentis_callback = cast(Callable[[], dict[str, Any]], init_agentis)
                adapter_steps.append(
                    self._run_adapter_step(
                        adapter,
                        kind="init_agentis",
                        success_message="Agentis konfigurace je připravená.",
                        callback=init_agentis_callback,
                    )
                )
            adapter_steps.append(
                self._run_adapter_step(
                    adapter,
                    kind="deploy",
                    started_message="Nasazuji prostředí do Kubernetes.",
                    success_message="Deploy do Kubernetes je hotový.",
                    callback=adapter.deploy,
                )
            )
            wait_result = self._run_adapter_step(
                adapter,
                kind="wait_ready",
                started_message="Čekám na inicializaci podu.",
                success_message="Pod je připravený.",
                callback=adapter.wait_ready,
            )
            adapter_steps.append(wait_result)
            pod_url = wait_result.get("url")
            if not isinstance(pod_url, str) or not pod_url:
                raise RuntimeError("wait_ready did not return a usable pod URL")
            session_step = self._run_adapter_step(
                adapter,
                kind="start_session",
                started_message="Zakládám Agent session.",
                success_message="Agent session byla založena.",
                callback=lambda: adapter.start_session(
                    pod_url=pod_url, fork_from_session_id=params.fork_from_session_id
                ),
            )
            adapter_steps.append(session_step)
            session_id = context.session_id or session_step.get("session_id")
            if isinstance(session_id, str) and session_id:
                context.session_id = session_id
                run.opencode_session_id = session_id
                self.session_registry.register(session_id, context)
                snapshot_key = session_step.get("snapshot_key")
                if isinstance(snapshot_key, str):
                    self.session_registry.set_snapshot_key(session_id, snapshot_key)
        except Exception as exc:
            run.status = "failed"
            raise AgentJsonRpcException(500, f"Adapter error: {exc}") from exc
        return {
            "run": run.safe_dump(),
            "adapter": {
                "executed": True,
                "steps": adapter_steps,
            },
        }

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
        if params.context.session_id:
            self.session_registry.register(params.context.session_id, params.context)
            session_id = params.context.session_id
        else:
            raise AgentJsonRpcException(400, "Context must include session_id to add messages")

        context = params.context
        run = RunStatePayload(run_id=context.run_id, context=context)
        run.opencode_session_id = session_id
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

        adapter_steps: list[dict[str, Any]] = []
        try:
            adapter = self._adapter_factory(context)
            adapter_steps.append(
                self._run_adapter_step(
                    adapter,
                    kind="create_worktree",
                    started_message="Zakládám git worktree.",
                    success_message="Git worktree je připravený.",
                    callback=adapter.create_worktree,
                )
            )
            adapter_steps.append(
                self._run_adapter_step(
                    adapter,
                    kind="deploy",
                    started_message="Nasazuji prostředí do Kubernetes.",
                    success_message="Deploy do Kubernetes je hotový.",
                    callback=adapter.deploy,
                )
            )
            wait_result = self._run_adapter_step(
                adapter,
                kind="wait_ready",
                started_message="Čekám na inicializaci podu.",
                success_message="Pod je připravený.",
                callback=adapter.wait_ready,
            )
            adapter_steps.append(wait_result)
            pod_url = wait_result.get("url")
            if not isinstance(pod_url, str) or not pod_url:
                raise RuntimeError("wait_ready did not return a usable pod URL")
            session_step = self._run_adapter_step(
                adapter,
                kind="start_session",
                started_message="Přidávám zprávu OpenCode session.",
                success_message="Zpráva do session byla založena.",
                callback=lambda: adapter.add_message(params.message, pod_url=pod_url),
            )
            adapter_steps.append(session_step)
            snapshot_key = session_step.get("snapshot_key")
            if isinstance(snapshot_key, str):
                self.session_registry.set_snapshot_key(session_id, snapshot_key)
        except Exception as exc:
            run.status = "failed"
            raise AgentJsonRpcException(500, f"Adapter error: {exc}") from exc
        return {
            "run": run.safe_dump(),
            "adapter": {
                "executed": True,
                "steps": adapter_steps,
            },
        }

    def question(self, params: QuestionParams) -> dict[str, Any]:
        if params.context.session_id:
            self.session_registry.register(params.context.session_id, params.context)
            session_id = params.context.session_id
        else:
            raise AgentJsonRpcException(400, "Context must include session_id to reply to questions")

        context = params.context
        run = RunStatePayload(run_id=context.run_id, context=context)
        run.opencode_session_id = session_id
        run.events.append(
            RunEventPayload(
                kind="question",
                payload={"request_id": params.request_id, "answers": params.answers},
            )
        )

        adapter_steps: list[dict[str, Any]] = []
        try:
            adapter = self._adapter_factory(context)
            adapter_steps.append(
                self._run_adapter_step(
                    adapter,
                    kind="create_worktree",
                    started_message="Zakládám git worktree.",
                    success_message="Git worktree je připravený.",
                    callback=adapter.create_worktree,
                )
            )
            adapter_steps.append(
                self._run_adapter_step(
                    adapter,
                    kind="deploy",
                    started_message="Nasazuji prostředí do Kubernetes.",
                    success_message="Deploy do Kubernetes je hotový.",
                    callback=adapter.deploy,
                )
            )
            wait_result = self._run_adapter_step(
                adapter,
                kind="wait_ready",
                started_message="Čekám na inicializaci podu.",
                success_message="Pod je připravený.",
                callback=adapter.wait_ready,
            )
            adapter_steps.append(wait_result)
            pod_url = wait_result.get("url")
            if not isinstance(pod_url, str) or not pod_url:
                raise RuntimeError("wait_ready did not return a usable pod URL")
            adapter_steps.append(
                self._run_adapter_step(
                    adapter,
                    kind="question",
                    started_message="Předávám odpověď na otázku OpenCode session.",
                    success_message="Odpověď na otázku byla předána.",
                    callback=lambda: adapter.question_reply(params.request_id, params.answers, pod_url=pod_url),
                )
            )
        except Exception as exc:
            run.status = "failed"
            raise AgentJsonRpcException(500, f"Adapter error: {exc}") from exc
        return {
            "run": run.safe_dump(),
            "adapter": {
                "executed": True,
                "steps": adapter_steps,
            },
        }

    def approve(self, params: ApproveParams) -> dict[str, Any]:
        event = RunEventPayload(
            kind="approve",
            payload={"approved": params.approved, "comment": params.comment},
        )
        return {"ok": True, "approved": params.approved, "event": event.model_dump()}

    def abort(self, params: AbortParams) -> dict[str, Any]:
        context = params.context
        session_id = context.session_id
        if not session_id:
            raise AgentJsonRpcException(400, "Context must include session_id to abort session")

        run = RunStatePayload(run_id=context.run_id, context=context)
        run.opencode_session_id = session_id
        try:
            adapter = self._adapter_factory(context)
            step = self._run_adapter_step(
                adapter,
                kind="abort",
                started_message="Zastavuji bezici OpenCode session.",
                success_message="OpenCode session byla zastavena.",
                callback=lambda: adapter.abort(session_id),
            )
        except Exception as exc:
            run.status = "failed"
            raise AgentJsonRpcException(500, f"Adapter error: {exc}") from exc

        self.session_registry.remove(session_id)

        return {
            "run": run.safe_dump(),
            "adapter": {
                "executed": True,
                "steps": [step],
            },
        }

    def undo(self, params: UndoParams) -> dict[str, Any]:
        context = params.context
        session_id = context.session_id
        if not session_id:
            raise AgentJsonRpcException(400, "Context must include session_id to undo changes")

        snapshot_key = self.session_registry.get_snapshot_key(session_id)
        if not snapshot_key:
            raise AgentJsonRpcException(400, f"No source snapshot is registered for session {session_id}")

        run = RunStatePayload(run_id=context.run_id, context=context)
        run.opencode_session_id = session_id
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

    def git_merge(self, params: GitMergeParams) -> dict[str, Any]:
        context = params.context
        run = RunStatePayload(run_id=context.run_id, context=context)
        adapter = self._adapter_factory(context)
        try:
            git_merge_step = self._run_adapter_step(
                adapter,
                kind="git_merge",
                started_message="Mergeuji task větev do hlavní větve.",
                success_message="Task větev byla zmergována.",
                callback=lambda: adapter.git_merge(params.message),
            )
            step = self._run_adapter_step(
                adapter,
                kind="close",
                started_message="Uklízím Kubernetes namespace a git větev.",
                success_message="Prostředí bylo uklizeno.",
                callback=adapter.close,
            )
        except Exception as exc:
            run.status = "failed"
            raise AgentJsonRpcException(500, f"Adapter error: {exc}") from exc

        if context.run_id:
            body = "✔️ Zamergoval jsem task větev do hlavní větve a uklidil prostředí."
            conflict_resolution_output = git_merge_step.get("conflict_resolution_output")
            if "conflict_resolution_output" in git_merge_step:
                body += "\n\nMerge narazil na git conflict, který jsem řešil přes AI resolver."
                if isinstance(conflict_resolution_output, str) and conflict_resolution_output.strip():
                    body += f"\n\nVýsledek AI resolveru:\n\n```\n{conflict_resolution_output.strip()}\n```"
                else:
                    body += "\n\nAI resolver nevrátil žádný textový výstup."
            self.call_agentis(
                method="task.add_agent_comment",
                params={
                    "run_id": context.run_id,
                    "body": body,
                    "status": TaskStatus.DONE,
                },
            )

        if context.session_id:
            self.session_registry.remove(context.session_id)

        return {
            "run": run.safe_dump(),
            "adapter": {
                "executed": True,
                "steps": [step],
            },
        }

    def close(self, params: CloseParams) -> dict[str, Any]:
        context = params.context
        run = RunStatePayload(run_id=context.run_id, context=context)
        try:
            adapter = self._adapter_factory(context)
            step = self._run_adapter_step(
                adapter,
                kind="close",
                started_message="Uklízím Kubernetes namespace a git větev.",
                success_message="Prostředí bylo uklizeno.",
                callback=adapter.close,
            )
        except Exception as exc:
            run.status = "failed"
            raise AgentJsonRpcException(500, f"Adapter error: {exc}") from exc

        if context.run_id:
            self.call_agentis(
                method="task.add_agent_comment",
                params={
                    "run_id": context.run_id,
                    "body": "Kubernetes namespace a git větev byly uklizeny.",
                    "status": TaskStatus.CANCELLED,
                },
            )

        if context.session_id:
            self.session_registry.remove(context.session_id)

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
