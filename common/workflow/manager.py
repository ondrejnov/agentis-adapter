"""Background orchestrace deklarativního workflow režimu.

`WorkflowManager` drží běžící workflow runy per task, spouští jednotlivé kroky
jako Kubernetes Joby přes :class:`KubectlJobRunner` a po úspěšném dokončení
celého workflow aplikuje `outputs` do Agentisu. `start` / `add_message` vrací
rychle — workflow běží v daemon threadu.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.config import Settings
from common.git_adapter import GitAdapterService
from common.kubernetes.runtime import KubernetesRuntime
from common.models import AgentExecutionContextPayload
from common.session_manager import BaseSessionManager
from common.workflow.runtime import KubectlJobRunner, build_job_manifest, job_labels, job_name, safe_step_name
from common.workflow.schema import (
    PROJECT_WORKFLOW_FILE_RELPATH,
    WORKFLOW_FILE_RELPATH,
    WorkflowFile,
    WorkflowOutput,
    load_workflow_file,
)

_LOG_TAIL_LINES = 50


class WorkflowBusyError(RuntimeError):
    pass


@dataclass
class _WorkflowRun:
    context: AgentExecutionContextPayload
    worktree: Path
    workflow: WorkflowFile
    namespace: str
    attempt_id: str
    run_dir: Path
    output_root: Path
    prompt_file: Path
    context_file: Path
    abort_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    status: str = "running"

    @property
    def active(self) -> bool:
        return self.status == "running" and (self.thread is None or self.thread.is_alive())


class WorkflowManager:
    """Owns background workflow runs keyed by task_id."""

    def __init__(self, settings: Settings, runner: KubectlJobRunner | None = None) -> None:
        self.settings = settings
        self._runner = runner or KubectlJobRunner(settings)
        self._runs: dict[str, _WorkflowRun] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_workflow(
        self,
        context: AgentExecutionContextPayload,
        worktree: str,
        prompt: str,
    ) -> dict[str, Any]:
        """Připraví run, načte a zmrazí workflow YAML a spustí workflow na pozadí.

        Pro scope=project se použije `project.yaml` místo `ci.yaml` a run soubory
        (prompt, context, outputs) se zapisují mimo projekt do
        `<project_run_root>/<run_id>/<attempt>/`, aby mohlo běžet víc runů zároveň.
        """

        namespace = KubernetesRuntime.namespace_for_context(context, self.settings)
        task_label = self._task_label(context)
        with self._lock:
            existing = self._runs.get(context.task_id)
            if existing is not None and existing.active:
                raise WorkflowBusyError(f"Workflow for task {context.task_id} is already running")
        if self._runner.has_active_jobs(namespace, task_label):
            raise WorkflowBusyError(f"Workflow jobs for task {context.task_id} are still active in {namespace}")

        worktree_path = Path(worktree)
        attempt_id = uuid4().hex[:8]
        is_project_scope = GitAdapterService.is_project_scope(context)
        workflow_relpath = PROJECT_WORKFLOW_FILE_RELPATH if is_project_scope else WORKFLOW_FILE_RELPATH
        workflow_path = worktree_path / workflow_relpath
        if is_project_scope and not workflow_path.is_file():
            raise FileNotFoundError(
                f"Project scope vyžaduje workflow soubor {PROJECT_WORKFLOW_FILE_RELPATH}, "
                f"ale {workflow_path} neexistuje"
            )

        if is_project_scope:
            run_dir = self.settings.project_run_root / context.run_id / attempt_id
        else:
            run_dir = worktree_path / ".agentis" / "runs" / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = run_dir / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        context_file = run_dir / "context.json"
        context_dump = context.model_dump(mode="json")
        context_file.write_text(json.dumps(context_dump, ensure_ascii=False, indent=2), encoding="utf-8")

        values = self._interpolation_values(context, worktree_path, namespace, run_dir=run_dir)
        workflow = load_workflow_file(workflow_path, values)

        run = _WorkflowRun(
            context=context,
            worktree=worktree_path,
            workflow=workflow,
            namespace=namespace,
            attempt_id=attempt_id,
            run_dir=run_dir,
            output_root=run_dir if is_project_scope else worktree_path,
            prompt_file=prompt_file,
            context_file=context_file,
        )
        with self._lock:
            self._runs[context.task_id] = run

        thread = threading.Thread(
            target=self._thread_main,
            args=(run,),
            name=f"workflow-{context.task_id}-{attempt_id}",
            daemon=True,
        )
        run.thread = thread
        thread.start()

        return {
            "action": "workflow_start",
            "task_id": context.task_id,
            "attempt": attempt_id,
            "namespace": namespace,
            "workflow_file": workflow_relpath,
            "steps": [step.name for step in workflow.workflow.steps],
        }

    def abort(self, context: AgentExecutionContextPayload) -> dict[str, Any]:
        """Zruší workflow: smaže aktivní Joby podle labels (bez session_id)."""

        namespace = KubernetesRuntime.namespace_for_context(context, self.settings)
        with self._lock:
            run = self._runs.get(context.task_id)
        if run is not None:
            run.abort_event.set()
            run.status = "aborted"

        labels = {
            "agentis.task_id": self._task_label(context),
            "agentis.run_id": self._run_label(context),
        }
        deleted = self._runner.delete_jobs_by_labels(namespace, labels)
        self._emit_adapter_event(
            context,
            kind="workflow_abort",
            status="success",
            event_id=f"workflow_abort:{context.run_id}:{uuid4().hex}",
            message="Workflow bylo zastaveno, Joby byly smazány.",
            data={"namespace": namespace, "deleted": deleted},
        )
        return {
            "action": "abort",
            "task_id": context.task_id,
            "namespace": namespace,
            "deleted": deleted,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _task_label(context: AgentExecutionContextPayload) -> str:
        return safe_step_name(context.task_id) or "task"

    @staticmethod
    def _run_label(context: AgentExecutionContextPayload) -> str:
        return re.sub(r"[^a-z0-9-]", "-", context.run_id.lower())[:63].strip("-") or "run"

    def _interpolation_values(
        self,
        context: AgentExecutionContextPayload,
        worktree: Path,
        namespace: str,
        run_dir: Path | None = None,
    ) -> dict[str, str]:
        try:
            branch = GitAdapterService._branch_name_for_context(context)
        except RuntimeError:
            branch = ""
        return {
            "NAMESPACE": namespace,
            "WORKDIR": str(worktree),
            "RUN_DIR": str(run_dir) if run_dir is not None else "",
            "MAIN_DIR": context.working_dir or "",
            "RUN_ID": context.run_id,
            "TASK_ID": context.task_id,
            "TASK_NUMBER": str(context.task_number) if context.task_number is not None else "",
            "TASK_TITLE": context.title or "",
            "BRANCH": branch,
            "BASE_BRANCH": context.base_branch or "",
            "GITHUB_REPO": context.project_github_repo or "",
        }

    def _runtime_env(self, run: _WorkflowRun) -> dict[str, str]:
        values = self._interpolation_values(run.context, run.worktree, run.namespace, run_dir=run.run_dir)
        env = dict(values)
        env.update(
            {
                "AGENTIS_RUN_ID": run.context.run_id,
                "AGENTIS_TASK_ID": run.context.task_id,
                "AGENTIS_RUN_DIR": str(run.run_dir),
                "AGENTIS_PROMPT_FILE": str(run.prompt_file),
                "AGENTIS_CONTEXT_FILE": str(run.context_file),
            }
        )
        adapter = run.context.adapter
        if run.context.session_id:
            env["AGENTIS_SESSION_ID"] = run.context.session_id
        if adapter and adapter.model:
            env["AGENTIS_MODEL"] = adapter.model
        if adapter and adapter.agent:
            env["AGENTIS_AGENT"] = adapter.agent
        if adapter and adapter.effort:
            env["AGENTIS_EFFORT"] = adapter.effort
        return env

    def _thread_main(self, run: _WorkflowRun) -> None:
        try:
            self._run_workflow(run)
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            sys.stderr.write(f"[workflow] run {run.context.run_id} crashed: {exc!r}\n")
            self._emit_adapter_event(
                run.context,
                kind="workflow",
                status="failed",
                event_id=f"workflow:{run.context.run_id}:{run.attempt_id}",
                message="Workflow běh selhal.",
                data={"error": str(exc)},
            )

    def _run_workflow(self, run: _WorkflowRun) -> None:
        env = self._runtime_env(run)
        self._runner.ensure_namespace(run.namespace)
        workflow_event_id = f"workflow:{run.context.run_id}:{run.attempt_id}"
        self._emit_adapter_event(
            run.context,
            kind="workflow",
            status="started",
            event_id=workflow_event_id,
            message="Workflow bylo spuštěno.",
            data={"attempt": run.attempt_id, "namespace": run.namespace},
        )

        for index, step in enumerate(run.workflow.workflow.steps):
            if run.abort_event.is_set():
                run.status = "aborted"
                return

            labels = job_labels(
                task_id=run.context.task_id,
                run_id=run.context.run_id,
                attempt_id=run.attempt_id,
                step_index=index,
                step_name=step.name,
            )
            name = job_name(run.context.run_id, run.attempt_id, index, step.name)
            manifest = build_job_manifest(
                run.workflow,
                step,
                namespace=run.namespace,
                name=name,
                labels=labels,
                env=env,
            )
            step_event_id = f"workflow_step:{run.context.run_id}:{run.attempt_id}:{index}"
            self._emit_adapter_event(
                run.context,
                kind="workflow_step",
                status="started",
                event_id=step_event_id,
                message=step.name,
                data={"step": step.name, "job": name},
            )

            timeout = step.timeoutSeconds if step.timeoutSeconds is not None else run.workflow.workflow.timeoutSeconds
            self._runner.apply_job(manifest)
            result = self._runner.wait_for_job(
                run.namespace,
                name,
                timeout=float(timeout),
                abort_event=run.abort_event,
            )
            if result == "aborted":
                run.status = "aborted"
                return
            if result != "succeeded":
                run.status = "failed"
                log_tail = self._runner.job_log_tail(run.namespace, name, lines=_LOG_TAIL_LINES)
                self._emit_adapter_event(
                    run.context,
                    kind="workflow_step",
                    status="failed",
                    event_id=step_event_id,
                    message=f"Krok selhal ({result}): {step.name}",
                    data={"step": step.name, "job": name, "result": result, "log_tail": log_tail},
                )
                self._emit_adapter_event(
                    run.context,
                    kind="idle",
                    status="failed",
                    event_id=workflow_event_id,
                    message="Workflow selhalo.",
                    data={"failed_step": step.name, "result": result},
                )
                return

            self._emit_adapter_event(
                run.context,
                kind="workflow_step",
                status="success",
                event_id=step_event_id,
                message=step.name,
                data={"step": step.name, "job": name},
            )

        # Outputs se aplikují až po úspěšném dokončení celého workflow.
        self._apply_outputs(run)
        run.status = "success"
        self._emit_adapter_event(
            run.context,
            kind="idle",
            status="success",
            event_id=workflow_event_id,
            message="Workflow doběhlo.",
            data={"attempt": run.attempt_id},
        )

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------

    def _read_output_file(self, run: _WorkflowRun, relpath: str | None) -> str | None:
        if not relpath:
            return None
        path = (run.output_root / relpath).resolve()
        if run.output_root.resolve() not in path.parents:
            return None
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _apply_outputs(self, run: _WorkflowRun) -> None:
        outputs: list[WorkflowOutput] = []
        for step in run.workflow.workflow.steps:
            outputs.extend(step.outputs)

        comment_body: str | None = None
        comment_status: int | None = None
        session_id: str | None = None
        attachments: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []

        for output in outputs:
            if output.type == "agent_comment":
                body = self._read_output_file(run, output.bodyFrom)
                if body and body.strip():
                    comment_body = body.strip()
                    comment_status = output.status
            elif output.type == "session_id":
                value = self._read_output_file(run, output.valueFrom)
                if value and value.strip():
                    session_id = value.strip()
            elif output.type in {"url", "text"}:
                value = self._read_output_file(run, output.valueFrom)
                if value and value.strip():
                    attachments.append(
                        {
                            "label": output.label or output.type,
                            "value": value.strip(),
                            "type": output.type,
                        }
                    )
            elif output.type == "artifact":
                artifact = self._collect_artifact(run, output)
                if artifact is not None:
                    artifacts.append(artifact)

        if session_id:
            run.context.session_id = session_id
            self._agentis_call(
                method="run.store_session_id",
                params={"run_id": run.context.run_id, "session_id": session_id},
            )

        if comment_body:
            self._agentis_call(
                method="task.add_agent_comment",
                params={
                    "run_id": run.context.run_id,
                    "body": comment_body,
                    "attachments": attachments,
                    "artifacts": artifacts,
                    "status": comment_status,
                    "comment_type": "primary",
                    "actions": BaseSessionManager._completion_actions(run.context),
                },
            )
        elif attachments or artifacts:
            self._emit_adapter_event(
                run.context,
                kind="workflow_outputs",
                status="success",
                event_id=f"workflow_outputs:{run.context.run_id}:{run.attempt_id}",
                message="Workflow outputs byly zpracovány.",
                data={"attachments": attachments, "artifact_names": [item.get("name") for item in artifacts]},
            )

    def _collect_artifact(self, run: _WorkflowRun, output: WorkflowOutput) -> dict[str, Any] | None:
        if not output.path:
            return None
        path = (run.output_root / output.path).resolve()
        if run.output_root.resolve() not in path.parents or not path.is_file():
            return None
        try:
            content = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            return None
        return {
            "name": output.name or output.path,
            "filename": path.name,
            "content": content,
        }

    # ------------------------------------------------------------------
    # Agentis RPC
    # ------------------------------------------------------------------

    def _agentis_call(self, method: str, params: dict[str, Any]) -> None:
        endpoint = self.settings.agentis_endpoint
        if not endpoint:
            return
        try:
            with AgentisJsonRpcClient(endpoint=endpoint, token=self.settings.agentis_token) as client:
                client.call(method=method, params=params, request_id=f"workflow-{method}-{uuid4().hex}")
        except AgentisJsonRpcError as exc:
            sys.stderr.write(f"[workflow] agentis {method} failed: {exc}\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[workflow] agentis {method} unexpected error: {exc!r}\n")

    def _emit_adapter_event(
        self,
        context: AgentExecutionContextPayload,
        *,
        kind: str,
        status: str,
        event_id: str,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if not context.run_id:
            return
        self._agentis_call(
            method="run.adapter_event",
            params={
                "run_id": context.run_id,
                "kind": kind,
                "status": status,
                "event_id": event_id,
                "message": message,
                "data": data or {},
            },
        )


__all__ = ["WorkflowBusyError", "WorkflowManager"]
