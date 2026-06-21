"""Background orchestrace deklarativního workflow režimu.

`WorkflowManager` drží běžící workflow runy per task, spouští jednotlivé kroky
přes :class:`WorkflowStepRunner` (Kubernetes Joby přes :class:`KubectlJobRunner`,
nebo lokální bash procesy přes :class:`LocalProcessRunner` — podle executoru)
a po dokončení workflow aplikuje `outputs` úspěšně doběhlých kroků do Agentisu
(i po selhání — `always` krok tak může doručit failure komentář).
`start` / `add_message` vrací rychle — workflow běží v daemon threadu.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.artifacts.screenshots import collect_screenshot_images
from common.artifacts.source_snapshot import (
    build_snapshot_key,
    changes_diff_attachment,
    snapshot_sources_best_effort,
    write_changes_diff_best_effort,
)
from common.config import Settings
from common.git_adapter import GitAdapterService
from common.namespaces import namespace_for_context
from common.models import AgentExecutionContextPayload, task_header_env
from common.status import get_status_registry
from common.workflow.local_runtime import LocalProcessRunner
from common.workflow.runtime import (
    StepResult,
    KubectlJobRunner,
    WorkflowStepRunner,
    job_labels,
    job_name,
    safe_step_name,
)
from common.workflow.schema import (
    PROJECT_WORKFLOW_FILE_RELPATH,
    WORKFLOW_EXECUTORS,
    WORKFLOW_FILE_RELPATH,
    WorkflowFile,
    WorkflowOutput,
    WorkflowStep,
    evaluate_condition,
    load_workflow_file,
    workflow_file_relpath,
)


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
    executor: str
    runner: WorkflowStepRunner
    #: Klíč snapshotu zdrojáků pro "Changes diff" attachment; None pro pojmenovaná
    #: workflow (merge/close), která můžou worktree sama smazat.
    snapshot_key: Optional[str] = None
    abort_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    status: str = "running"
    #: Proměnné nasbírané z `var` outputs dokončených kroků; vstup pro `if` podmínky.
    vars: dict[str, str] = field(default_factory=dict)
    #: Indexy kroků přeskočených kvůli `if` nebo po selhání workflow — jejich
    #: outputs se na konci neaplikují.
    skipped_steps: set[int] = field(default_factory=set)
    #: Indexy selhaných kroků (`continueOnError` i fatální) — outputs se neaplikují.
    failed_steps: set[int] = field(default_factory=set)

    @property
    def active(self) -> bool:
        return self.status == "running" and (self.thread is None or self.thread.is_alive())


@dataclass(frozen=True)
class _StepExecutionResult:
    index: int
    step_name: str
    job_name: str
    result: StepResult
    attempts: int


class WorkflowManager:
    """Owns background workflow runs keyed by task_id."""

    def __init__(self, settings: Settings, runner: WorkflowStepRunner | None = None) -> None:
        self.settings = settings
        #: Explicitní runner (testy) má přednost před výběrem podle executoru.
        self._runner_override = runner
        self._runners: dict[str, WorkflowStepRunner] = {}
        self._runs: dict[str, _WorkflowRun] = {}
        self._lock = threading.Lock()

    def _runner_for(self, executor: str) -> WorkflowStepRunner:
        if self._runner_override is not None:
            return self._runner_override
        if executor not in WORKFLOW_EXECUTORS:
            raise ValueError(f"Unknown workflow executor {executor!r}; expected one of {WORKFLOW_EXECUTORS}")
        runner = self._runners.get(executor)
        if runner is None:
            runner = LocalProcessRunner(self.settings) if executor == "local" else KubectlJobRunner(self.settings)
            self._runners[executor] = runner
        return runner

    def _resolve_executor(self, context: AgentExecutionContextPayload, workflow: WorkflowFile) -> str:
        """Vybere executor pro run.

        Runtime `local` (`context.adapter.runtime`) vynutí lokální executor bez
        ohledu na `workflow.executor` / `WORKFLOW_EXECUTOR` — runtime je
        autoritativní signál prostředí. Jinak platí YAML `executor`, pak env
        default (`settings.workflow_executor`, default `kubernetes`).
        """

        runtime = (context.adapter.runtime if context.adapter and context.adapter.runtime else "").strip().lower()
        if runtime == "local":
            return "local"
        return (workflow.workflow.executor or self.settings.workflow_executor).strip().lower()

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

        Bez pojmenovaného workflow v kontextu se použije `default.yaml`, pro
        scope=project `project.yaml`. `context.adapter.workflow` (followup akce
        jako merge/close) vybírá `.agentis/workflows/<name>.yaml`. Run soubory
        (prompt, context, outputs) se pro scope=project a pojmenovaná workflow
        zapisují mimo worktree do `<project_run_root>/<run_id>/<attempt>/` —
        akce můžou worktree samy smazat.
        """

        namespace = namespace_for_context(context, self.settings)
        task_label = self._task_label(context)
        with self._lock:
            existing = self._runs.get(context.task_id)
            if existing is not None and existing.active:
                raise WorkflowBusyError(f"Workflow for task {context.task_id} is already running")

        worktree_path = Path(worktree)
        # Hex timestamp s pevnou šířkou: lexikografické řazení názvů jobů odpovídá pořadí spuštění.
        attempt_id = f"{time.time_ns() // 1_000_000:011x}"
        is_project_scope = GitAdapterService.is_project_scope(context)
        workflow_name = self._workflow_name(context)
        if workflow_name:
            workflow_relpath = workflow_file_relpath(workflow_name)
        else:
            workflow_relpath = PROJECT_WORKFLOW_FILE_RELPATH if is_project_scope else WORKFLOW_FILE_RELPATH
        # Workflow soubor: přednost má projektový (`.agentis/workflows/...`), při jeho
        # absenci fallback na předpřipravené workflow zabalené v adapteru
        # (`settings.bundled_workflow_dir`, default `workflows/` v rootu repa) — hledá se
        # podle basename. `extends: _base` se i ve fallbacku vyřeší relativně k souboru,
        # takže `_base.yaml` v té samé složce funguje. Bez obou variant run nemá co
        # spustit a vrací se chyba do Agentisu (žádný fallback na CLI session).
        workflow_path = worktree_path / workflow_relpath
        if not workflow_path.is_file():
            bundled_path = self.settings.bundled_workflow_dir / Path(workflow_relpath).name
            if bundled_path.is_file():
                workflow_path = bundled_path
            elif workflow_name:
                raise FileNotFoundError(
                    f"Workflow {workflow_name!r} vyžaduje soubor {workflow_relpath} v projektu "
                    f"({workflow_path}) nebo zabalený fallback ({bundled_path}), ale ani jeden neexistuje"
                )
            else:
                raise FileNotFoundError(
                    f"Projekt nemá workflow soubor {workflow_relpath} ({workflow_path}) "
                    f"ani zabalený fallback ({bundled_path}); run přes workflow runtime nelze spustit"
                )

        external_run_files = is_project_scope or workflow_name is not None
        if external_run_files:
            run_dir = self.settings.project_run_root / context.run_id / attempt_id
        else:
            run_dir = worktree_path / ".agentis" / "runs" / attempt_id

        values = self._interpolation_values(context, worktree_path, namespace, run_dir=run_dir)
        workflow = load_workflow_file(workflow_path, values)
        executor = self._resolve_executor(context, workflow)
        runner = self._runner_for(executor)
        if executor == "kubernetes":
            self._require_images(workflow, workflow_relpath)
        if runner.has_active_run(namespace, task_label):
            raise WorkflowBusyError(f"Workflow jobs for task {context.task_id} are still active in {namespace}")

        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = run_dir / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        context_file = run_dir / "context.json"
        context_dump = context.model_dump(mode="json")
        context_file.write_text(json.dumps(context_dump, ensure_ascii=False, indent=2), encoding="utf-8")

        run = _WorkflowRun(
            context=context,
            worktree=worktree_path,
            workflow=workflow,
            namespace=namespace,
            attempt_id=attempt_id,
            run_dir=run_dir,
            output_root=run_dir if external_run_files else worktree_path,
            prompt_file=prompt_file,
            context_file=context_file,
            executor=executor,
            runner=runner,
            snapshot_key=(
                None if workflow_name else build_snapshot_key("workflow", context.run_id, context.task_id, attempt_id)
            ),
        )
        with self._lock:
            self._runs[context.task_id] = run

        get_status_registry().run_update(
            context.run_id,
            kind="workflow",
            worktree=str(worktree_path),
            workflow=workflow_name,
        )

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
            "executor": executor,
            "workflow": workflow_name,
            "workflow_file": workflow_relpath,
            "steps": [step.name for step in workflow.workflow.steps],
        }

    def snapshot_key_for_task(self, task_id: str) -> str | None:
        """Klíč source snapshotu posledního runu tasku (pro `undo`); None pro pojmenovaná workflow."""

        with self._lock:
            run = self._runs.get(task_id)
            return run.snapshot_key if run is not None else None

    def abort(self, context: AgentExecutionContextPayload) -> dict[str, Any]:
        """Zruší workflow: zastaví aktivní kroky podle labels (bez session_id)."""

        namespace = namespace_for_context(context, self.settings)
        with self._lock:
            run = self._runs.get(context.task_id)
        if run is not None:
            run.abort_event.set()
            run.status = "aborted"

        labels = {
            "agentis.task_id": self._task_label(context),
            "agentis.run_id": self._run_label(context),
        }
        runner = run.runner if run is not None else self._runner_for(self.settings.workflow_executor)
        deleted = runner.abort(namespace, labels)
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

    def active_count(self) -> int:
        """Počet workflow runů, jejichž thready stále běží (pro graceful shutdown)."""
        return len(self._active_threads())

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Blokuje, dokud nedoběhnou všechny workflow thready.

        Vrací ``False``, pokud po ``timeout`` sekundách stále něco běží;
        ``timeout=None`` čeká bez limitu.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            threads = self._active_threads()
            if not threads:
                return True
            if deadline is None:
                threads[0].join()
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            threads[0].join(timeout=remaining)

    def _active_threads(self) -> list[threading.Thread]:
        with self._lock:
            threads = [run.thread for run in self._runs.values() if run.thread is not None]
        return [thread for thread in threads if thread.is_alive()]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _require_images(workflow: WorkflowFile, workflow_relpath: str) -> None:
        """Executor `kubernetes` potřebuje image pro každý krok; lokální executor je ignoruje."""

        spec = workflow.workflow
        missing = [step.name for step in spec.steps if not (step.image or spec.image)]
        if missing:
            raise ValueError(
                f"Workflow executor 'kubernetes' vyžaduje 'image' v {workflow_relpath} "
                f"(chybí pro kroky: {', '.join(missing)})"
            )

    @staticmethod
    def _workflow_name(context: AgentExecutionContextPayload) -> str | None:
        if context.adapter and context.adapter.workflow:
            return context.adapter.workflow
        return None

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
        env.update(task_header_env(run.context.headers))
        env.update(
            {
                "AGENTIS_RUN_ID": run.context.run_id,
                "AGENTIS_TASK_ID": run.context.task_id,
                "AGENTIS_RUN_DIR": str(run.run_dir),
                "AGENTIS_PROMPT_FILE": str(run.prompt_file),
                "AGENTIS_CONTEXT_FILE": str(run.context_file),
            }
        )
        if self.settings.agentis_endpoint:
            env["AGENTIS_ENDPOINT"] = self.settings.agentis_endpoint
        if self.settings.agentis_service_token:
            env["AGENTIS_SERVICE_TOKEN"] = self.settings.agentis_service_token
        adapter = run.context.adapter
        if run.context.session_id:
            env["AGENTIS_SESSION_ID"] = run.context.session_id
        if adapter and adapter.model:
            env["AGENTIS_MODEL"] = adapter.model
        if adapter and adapter.agent:
            env["AGENTIS_AGENT"] = adapter.agent
        if adapter and adapter.effort:
            env["AGENTIS_EFFORT"] = adapter.effort
        if adapter:
            env["AGENTIS_AUTO_MERGE"] = "true" if adapter.auto_merge else "false"
        return env

    def _thread_main(self, run: _WorkflowRun) -> None:
        try:
            self._run_workflow(run)
            get_status_registry().run_finished(run.context.run_id, run.status)
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            get_status_registry().run_finished(run.context.run_id, "failed")
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
        if run.snapshot_key:
            snapshot_sources_best_effort(run.worktree, run.snapshot_key, label="workflow-start")
        env = self._runtime_env(run)
        #: Built-in hodnoty (INTERPOLATION_ALLOWLIST) dostupné v `if` podmínkách;
        #: `var` output stejného jména z kroku built-in hodnotu přepisuje.
        builtin_vars = self._interpolation_values(run.context, run.worktree, run.namespace, run_dir=run.run_dir)
        run.runner.prepare(run.workflow, namespace=run.namespace, run_dir=run.run_dir)
        workflow_event_id = f"workflow:{run.context.run_id}:{run.attempt_id}"
        self._emit_adapter_event(
            run.context,
            kind="workflow",
            status="success",
            event_id=workflow_event_id,
            message="Workflow bylo spuštěno.",
            data={"attempt": run.attempt_id, "namespace": run.namespace, "executor": run.executor},
        )

        steps = run.workflow.workflow.steps
        dependencies = self._step_dependencies(steps)
        step_vars: dict[int, dict[str, str]] = {}
        terminal_steps: set[int] = set()
        pending_steps: set[int] = set(range(len(steps)))
        running_steps: dict[Future[_StepExecutionResult], int] = {}

        #: Jméno prvního fatálně selhaného kroku; po selhání běží už jen `always` kroky.
        failed_step: str | None = None
        max_parallel = run.workflow.workflow.maxParallel
        with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="workflow-step") as executor:
            while pending_steps or running_steps:
                if run.abort_event.is_set():
                    pending_steps.clear()
                    if not running_steps:
                        run.status = "aborted"
                        return

                progressed = False
                if failed_step is not None:
                    for index in sorted(list(pending_steps)):
                        step = steps[index]
                        if step.always:
                            continue
                        pending_steps.remove(index)
                        terminal_steps.add(index)
                        run.skipped_steps.add(index)
                        self._emit_step_skipped(run, index, step, dependencies, failed_step=failed_step)
                        progressed = True

                active_regular_steps = any(not steps[index].always for index in running_steps.values())
                for index in sorted(list(pending_steps)):
                    if len(running_steps) >= max_parallel:
                        break
                    step = steps[index]
                    if failed_step is not None and not step.always:
                        continue
                    if failed_step is not None and step.always and active_regular_steps:
                        continue
                    if any(dependency not in terminal_steps for dependency in dependencies[index]):
                        continue

                    dependency_vars = self._dependency_vars(index, dependencies, step_vars)
                    condition_vars = {
                        **run.workflow.workflow.env,
                        **env,
                        **step.env,
                        **builtin_vars,
                        **dependency_vars,
                    }
                    if step.if_ is not None and not evaluate_condition(step.if_, condition_vars):
                        pending_steps.remove(index)
                        terminal_steps.add(index)
                        run.skipped_steps.add(index)
                        self._emit_step_skipped(
                            run,
                            index,
                            step,
                            dependencies,
                            condition=step.if_,
                            visible_vars=dependency_vars,
                        )
                        progressed = True
                        continue

                    self._emit_step_started(run, index, step, dependencies)
                    step_env = {**env, **dependency_vars}
                    if step.always:
                        step_env = dict(step_env)
                        step_env["AGENTIS_WORKFLOW_STATUS"] = "failed" if failed_step else "success"
                        step_env["AGENTIS_FAILED_STEP"] = failed_step or ""
                    future = executor.submit(self._run_step_with_retries, run, index, step, step_env)
                    running_steps[future] = index
                    pending_steps.remove(index)
                    progressed = True

                if not running_steps:
                    if pending_steps and not progressed:
                        raise RuntimeError("workflow scheduler deadlocked; no runnable pending steps")
                    continue

                if progressed and len(running_steps) < max_parallel and pending_steps:
                    continue

                done, _pending = wait(running_steps, return_when=FIRST_COMPLETED)
                for future in done:
                    index = running_steps.pop(future)
                    terminal_steps.add(index)
                    step = steps[index]
                    try:
                        completed = future.result()
                    except Exception as exc:  # noqa: BLE001
                        completed = _StepExecutionResult(
                            index=index,
                            step_name=step.name,
                            job_name=job_name(run.context.run_id, run.attempt_id, index, step.name),
                            result=StepResult(status="failed", log_tail=str(exc)),
                            attempts=1,
                        )
                    if completed.result.status == "aborted":
                        run.abort_event.set()
                        pending_steps.clear()
                        run.status = "aborted"
                        continue
                    if completed.result.status != "succeeded":
                        run.failed_steps.add(index)
                        self._emit_step_failed(run, completed, step, dependencies)
                        if not step.continueOnError and failed_step is None:
                            failed_step = step.name
                        continue

                    self._emit_step_succeeded(run, completed, step, dependencies)
                    new_vars = self._collect_step_vars(run, step)
                    if new_vars:
                        step_vars[index] = new_vars
                        run.vars.update(new_vars)

        if run.status == "aborted":
            return

        # Outputs úspěšně doběhlých kroků se aplikují i po selhání workflow —
        # `always` krok tak může doručit failure komentář do ticketu.
        if failed_step is not None:
            run.status = "failed"
            self._apply_outputs(run)
            self._emit_adapter_event(
                run.context,
                kind="idle",
                status="failed",
                event_id=workflow_event_id,
                message="Workflow selhalo.",
                data={"failed_step": failed_step, "attempt": run.attempt_id},
            )
            return

        self._apply_outputs(run)
        self._cleanup_namespace(run)
        run.status = "success"
        self._emit_adapter_event(
            run.context,
            kind="idle",
            status="success",
            event_id=workflow_event_id,
            message="Workflow doběhlo.",
            data={"attempt": run.attempt_id},
        )

    @staticmethod
    def _step_dependencies(steps: list[WorkflowStep]) -> list[list[int]]:
        name_to_index: dict[str, int] = {}
        dependencies: list[list[int]] = []
        for index, step in enumerate(steps):
            if step.needs is None:
                dependencies.append([index - 1] if index > 0 else [])
            else:
                dependencies.append([name_to_index[name] for name in step.needs])
            name_to_index[step.name] = index
        return dependencies

    @staticmethod
    def _dependency_names(index: int, steps: list[WorkflowStep], dependencies: list[list[int]]) -> list[str]:
        return [steps[dependency].name for dependency in dependencies[index]]

    @staticmethod
    def _dependency_vars(
        index: int,
        dependencies: list[list[int]],
        step_vars: dict[int, dict[str, str]],
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        seen: set[int] = set()

        def visit(dependency: int) -> None:
            if dependency in seen:
                return
            for parent in dependencies[dependency]:
                visit(parent)
            seen.add(dependency)
            values.update(step_vars.get(dependency, {}))

        for dependency in dependencies[index]:
            visit(dependency)
        return values

    def _run_step_with_retries(
        self,
        run: _WorkflowRun,
        index: int,
        step: WorkflowStep,
        step_env: dict[str, str],
    ) -> _StepExecutionResult:
        labels = job_labels(
            task_id=run.context.task_id,
            run_id=run.context.run_id,
            attempt_id=run.attempt_id,
            step_index=index,
            step_name=step.name,
        )
        name = job_name(run.context.run_id, run.attempt_id, index, step.name)
        timeout = step.timeoutSeconds if step.timeoutSeconds is not None else run.workflow.workflow.timeoutSeconds
        attempt = 0
        while True:
            attempt += 1
            attempt_name = name if attempt == 1 else f"{name[:60].rstrip('-')}-r{attempt}"
            result = run.runner.run_step(
                run.workflow,
                step,
                namespace=run.namespace,
                name=attempt_name,
                labels=labels,
                env=step_env,
                timeout=float(timeout),
                abort_event=run.abort_event,
                run_dir=run.run_dir,
            )
            if result.status == "aborted" or result.status == "succeeded" or attempt > step.retries:
                return _StepExecutionResult(
                    index=index,
                    step_name=step.name,
                    job_name=name,
                    result=result,
                    attempts=attempt,
                )
            if run.abort_event.is_set():
                return _StepExecutionResult(
                    index=index,
                    step_name=step.name,
                    job_name=name,
                    result=StepResult(status="aborted"),
                    attempts=attempt,
                )

    def _emit_step_started(
        self,
        run: _WorkflowRun,
        index: int,
        step: WorkflowStep,
        dependencies: list[list[int]],
    ) -> None:
        name = job_name(run.context.run_id, run.attempt_id, index, step.name)
        self._emit_adapter_event(
            run.context,
            kind="workflow_step",
            status="started",
            event_id=f"workflow_step:{run.context.run_id}:{run.attempt_id}:{index}",
            message=step.name,
            data={
                "step": step.name,
                "step_index": index,
                "needs": self._dependency_names(index, run.workflow.workflow.steps, dependencies),
                "job": name,
            },
        )

    def _emit_step_succeeded(
        self,
        run: _WorkflowRun,
        completed: _StepExecutionResult,
        step: WorkflowStep,
        dependencies: list[list[int]],
    ) -> None:
        self._emit_adapter_event(
            run.context,
            kind="workflow_step",
            status="success",
            event_id=f"workflow_step:{run.context.run_id}:{run.attempt_id}:{completed.index}",
            message=step.name,
            data={
                "step": completed.step_name,
                "step_index": completed.index,
                "needs": self._dependency_names(completed.index, run.workflow.workflow.steps, dependencies),
                "job": completed.job_name,
            },
        )

    def _emit_step_failed(
        self,
        run: _WorkflowRun,
        completed: _StepExecutionResult,
        step: WorkflowStep,
        dependencies: list[list[int]],
    ) -> None:
        self._emit_adapter_event(
            run.context,
            kind="workflow_step",
            status="failed",
            event_id=f"workflow_step:{run.context.run_id}:{run.attempt_id}:{completed.index}",
            message=f"Krok selhal ({completed.result.status}): {step.name}",
            data={
                "step": completed.step_name,
                "step_index": completed.index,
                "needs": self._dependency_names(completed.index, run.workflow.workflow.steps, dependencies),
                "job": completed.job_name,
                "result": completed.result.status,
                "log_tail": completed.result.log_tail,
                "attempts": completed.attempts,
                "continueOnError": step.continueOnError,
            },
        )

    def _emit_step_skipped(
        self,
        run: _WorkflowRun,
        index: int,
        step: WorkflowStep,
        dependencies: list[list[int]],
        *,
        condition: str | None = None,
        visible_vars: dict[str, str] | None = None,
        failed_step: str | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "step": step.name,
            "step_index": index,
            "needs": self._dependency_names(index, run.workflow.workflow.steps, dependencies),
            "skipped": True,
        }
        if condition is not None:
            data["condition"] = condition
            data["vars"] = dict(visible_vars or {})
            message = f"Krok přeskočen (if: {condition}): {step.name}"
        else:
            data["failed_step"] = failed_step
            message = f"Krok přeskočen (workflow selhalo): {step.name}"
        self._emit_adapter_event(
            run.context,
            kind="workflow_step",
            status="skipped",
            event_id=f"workflow_step:{run.context.run_id}:{run.attempt_id}:{index}",
            message=message,
            data=data,
        )

    def _cleanup_namespace(self, run: _WorkflowRun) -> None:
        """Smaže namespace po úspěšném workflow s `deleteNamespace: true`.

        Jen pro executor `kubernetes` — lokální executor namespace nevytváří.
        Selhání úklidu workflow neshodí, jen se nahlásí do Agentisu.
        """

        if not run.workflow.workflow.deleteNamespace or run.executor != "kubernetes":
            return
        try:
            run.runner.delete_namespace(run.namespace)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[workflow] delete namespace {run.namespace} failed: {exc!r}\n")
            self._emit_adapter_event(
                run.context,
                kind="workflow_cleanup",
                status="failed",
                event_id=f"workflow_cleanup:{run.context.run_id}:{run.attempt_id}",
                message=f"Smazání namespace {run.namespace} selhalo.",
                data={"namespace": run.namespace, "error": str(exc)},
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

    def _collect_step_vars(self, run: _WorkflowRun, step: WorkflowStep) -> dict[str, str]:
        values: dict[str, str] = {}
        for output in step.outputs:
            if output.type != "var" or not output.name:
                continue
            value = self._read_output_file(run, output.valueFrom)
            values[output.name] = (value or "").strip()
        return values

    def _apply_outputs(self, run: _WorkflowRun) -> None:
        outputs: list[WorkflowOutput] = []
        for index, step in enumerate(run.workflow.workflow.steps):
            if index in run.skipped_steps or index in run.failed_steps:
                continue
            outputs.extend(step.outputs)

        comments: list[dict[str, Any]] = []
        session_id: str | None = None
        attachments: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []

        ide = (run.context.ide or "").strip()
        if ide and run.snapshot_key and not GitAdapterService.is_project_scope(run.context):
            attachments.append(
                {
                    "label": "Directory",
                    "value": ide.replace("[%WORKDIR%]", str(run.worktree)),
                    "type": "url",
                }
            )

        for output in outputs:
            if output.type == "agent_comment":
                body = self._read_output_file(run, output.bodyFrom)
                if body and body.strip():
                    name = self._read_output_file(run, output.nameFrom) if output.nameFrom else output.name
                    comments.append(
                        {
                            "body": body.strip(),
                            "status": output.status,
                            "author_name": (name or "").strip() or None,
                        }
                    )
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

        if run.snapshot_key:
            diff_result = write_changes_diff_best_effort(run.worktree, run.snapshot_key, label="workflow-finish")
            diff_attachment = changes_diff_attachment(diff_result)
            if diff_attachment:
                attachments.append(diff_attachment)

        if session_id:
            run.context.session_id = session_id
            self._agentis_call(
                method="run.store_session_id",
                params={"run_id": run.context.run_id, "session_id": session_id},
            )

        if comments:
            # Followup akce se konfigurují v `workflow.followups` sekci workflow YAML;
            # pojmenovaná workflow (merge/close) sekci nemají, takže další akce nenabízí.
            # U failure komentáře se akce nenabízí vůbec — merge/close rozdělané práce
            # po selhaném runu nedává smysl. Followup s `if` se nabídne jen při splnění
            # podmínky nad `var` outputs runu; bez podmínky se nabízí vždy.
            actions = (
                []
                if run.status == "failed"
                else [
                    followup.to_action()
                    for followup in run.workflow.workflow.followups
                    if followup.if_ is None or evaluate_condition(followup.if_, run.vars)
                ]
            )
            images = collect_screenshot_images(run.worktree)
            last_comment_index = len(comments) - 1
            for index, comment in enumerate(comments):
                include_run_outputs = index == last_comment_index
                self._agentis_call(
                    method="task.add_agent_comment",
                    params={
                        "run_id": run.context.run_id,
                        "body": comment["body"],
                        "attachments": attachments if include_run_outputs else [],
                        "images": images if include_run_outputs else [],
                        "artifacts": artifacts if include_run_outputs else [],
                        "status": comment["status"],
                        "comment_type": "primary",
                        "actions": actions if include_run_outputs else [],
                        "author_name": comment["author_name"],
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
            with AgentisJsonRpcClient(
                endpoint=endpoint,
                token=self.settings.agentis_token,
                service_token=self.settings.agentis_service_token,
            ) as client:
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
        if message:
            get_status_registry().run_activity(context.run_id, message)
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
