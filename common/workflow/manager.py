"""Adapterová strana workflow runů: spouští engine `agentis-workflow` přes CLI a překládá jeho výsledky do Agentisu.

`WorkflowManager` vybere workflow soubor, namespace a run adresář, zapíše `spec.json`, ověří ho
`agentis-workflow check` a spustí `agentis-workflow run --spec ... --events jsonl` v child procesu
(vlastní process group). JSONL události převádí na `run.adapter_event` a status registry; po skončení
čte `result.json` a aplikuje outputs (komentáře, followup akce, session, přílohy, artefakty, changes diff).
Scheduler kroků, retry, závislosti a podmínky běží v enginu, adapter o nich neví nic víc než z událostí.
`start` / `add_message` vrací rychle — run běží na pozadí v reader threadu.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.artifacts.screenshots import clear_screenshot_images, collect_screenshot_images
from common.artifacts.source_snapshot import (
    build_snapshot_key,
    changes_diff_attachment,
    snapshot_sources_best_effort,
    write_changes_diff_best_effort,
)
from common.config import Settings
from common.git_adapter import GitAdapterService
from common.models import AgentExecutionContextPayload, ApproveResult, task_header_env
from common.namespaces import namespace_for_context
from common.status import get_status_registry
from common.workflow import engine_cli

ARTIFACT_OUTPUT_MAX_FILES = 50
ARTIFACT_OUTPUT_MAX_BYTES = 50 * 1024 * 1024
APPROVAL_HISTORY_MAX_ENTRIES = 1000
#: Interpolační tokeny, které nastavuje spec sám (z namespace, workdir, run_dir); ve `vars` nejsou povoleny.
ENGINE_OWNED_TOKENS = frozenset({"NAMESPACE", "WORKDIR", "RUN_DIR"})
#: Po SIGTERM počkáme na doběh enginu (úklid jobů); po uplynutí se vrací stav bez čekání.
ABORT_WAIT_SECONDS = 60.0
#: Posledních N řádků stderr enginu se drží kvůli diagnostice selhaného runu.
STDERR_TAIL_LINES = 20
DEFAULT_WORKFLOW_NAME = "default"
PROJECT_WORKFLOW_NAME = "project"
WORKFLOW_DIR_RELPATH = ".agentis/workflows"


class WorkflowBusyError(RuntimeError):
    pass


class WorkflowApprovalError(RuntimeError):
    pass


class WorkflowApprovalTimeoutError(WorkflowApprovalError):
    pass


@dataclass
class _WorkflowRun:
    context: AgentExecutionContextPayload
    worktree: Path
    namespace: str
    attempt_id: str
    run_dir: Path
    output_root: Path
    executor: str
    spec_path: Path
    #: Labely, podle kterých engine nachází a ruší joby tohoto runu (bez attempt — joby nese engine vlastní).
    labels: dict[str, str]
    task_label: str
    workflow_name: str | None
    workflow_relpath: str
    approve_metadata: str | None = None
    #: Klíč snapshotu zdrojáků pro "Changes diff" attachment; None pro pojmenovaná workflow
    #: (merge/close), která můžou worktree sama smazat, a pro approval runy.
    snapshot_key: Optional[str] = None
    #: Approval runy jsou vnořené do hlavního workflow a nesmí měnit jeho Agentis lifecycle.
    report_to_agentis: bool = True
    abort_event: threading.Event = field(default_factory=threading.Event)
    completion_event: threading.Event = field(default_factory=threading.Event)
    process: Optional[subprocess.Popen[str]] = None
    thread: Optional[threading.Thread] = None
    returncode: int | None = None
    status: str = "running"
    error: Exception | None = None
    #: Proměnné `var` outputs úspěšných kroků z `result.json`.
    vars: dict[str, str] = field(default_factory=dict)
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_TAIL_LINES))

    @property
    def active(self) -> bool:
        return self.status == "running" and (self.thread is None or self.thread.is_alive())


@dataclass
class _ApprovalEntry:
    fingerprint: str
    parent_run_id: str
    deadline: float
    completion_event: threading.Event = field(default_factory=threading.Event)
    run: _WorkflowRun | None = None
    error: Exception | None = None
    cancelled: bool = False


class WorkflowManager:
    """Owns background workflow runs keyed by task_id."""

    def __init__(
        self,
        settings: Settings,
        agentis_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        #: Náhrada Agentis RPC (testy): volání jdou sem místo na endpoint.
        self._agentis_sink = agentis_sink
        self._runs: dict[str, _WorkflowRun] = {}
        self._starting_tasks: set[str] = set()
        self._approval_runs: dict[str, _ApprovalEntry] = {}
        self._last_attempt_value = 0
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
        """Připraví run, ověří ho přes `check` a spustí engine na pozadí.

        Bez pojmenovaného workflow v kontextu se použije `default.yaml`, pro scope=project `project.yaml`.
        `context.adapter.workflow` (followup akce jako merge/close) vybírá `.agentis/workflows/<name>.yaml`.
        Run soubory (prompt, context, spec, outputs) se pro scope=project a pojmenovaná workflow zapisují
        mimo worktree do `<project_run_root>/<run_id>/<attempt>/` — akce můžou worktree samy smazat.
        """

        with self._lock:
            existing = self._runs.get(context.task_id)
            if context.task_id in self._starting_tasks or (existing is not None and existing.active):
                raise WorkflowBusyError(f"Workflow for task {context.task_id} is already running")
            self._starting_tasks.add(context.task_id)

        try:
            run, result = self._prepare(context, worktree, prompt)
            with self._lock:
                self._launch(run, name=f"workflow-{context.task_id}-{run.attempt_id}")
                self._runs[context.task_id] = run
            return result
        finally:
            with self._lock:
                self._starting_tasks.discard(context.task_id)

    def approve(
        self,
        context: AgentExecutionContextPayload,
        worktree: str,
        approval_id: str,
        metadata: dict[str, Any] | None,
        *,
        timeout: float = 300,
    ) -> int | dict[str, Any]:
        """Vrátí ``APPROVED`` a případná JSON metadata z ``APPROVED_METADATA``."""

        workflow_name = self._workflow_name(context)
        if workflow_name is None:
            raise ValueError("context.adapter.workflow is required for approve")
        metadata_json = json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        fingerprint = self._approval_fingerprint(context, metadata_json)
        with self._lock:
            entry = self._approval_runs.get(approval_id)
            owner = entry is None
            if entry is None:
                self._trim_approval_history_locked()
                entry = _ApprovalEntry(
                    fingerprint=fingerprint,
                    parent_run_id=context.run_id,
                    deadline=time.monotonic() + timeout,
                )
                self._approval_runs[approval_id] = entry
            elif entry.fingerprint != fingerprint:
                raise WorkflowApprovalError(f"Approval {approval_id!r} was already used with different input")

        if owner:
            try:
                run, _result = self._prepare(
                    context,
                    worktree,
                    None,
                    report_to_agentis=False,
                    completion_event=entry.completion_event,
                    approval=True,
                    approval_id=approval_id,
                    approve_metadata=metadata_json,
                )
                with self._lock:
                    if time.monotonic() >= entry.deadline:
                        entry.cancelled = True
                        entry.error = WorkflowApprovalTimeoutError(f"Approval {approval_id!r} timed out")
                        entry.completion_event.set()
                    elif not entry.cancelled:
                        entry.run = run
                        self._launch(run, name=f"approval-{approval_id}-{run.attempt_id}")
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    if not entry.cancelled:
                        entry.error = exc
                        entry.completion_event.set()

        remaining = max(0.0, entry.deadline - time.monotonic())
        if not entry.completion_event.wait(timeout=remaining):
            run_to_abort: _WorkflowRun | None = None
            with self._lock:
                entry.cancelled = True
                entry.error = WorkflowApprovalTimeoutError(f"Approval {approval_id!r} timed out")
                run = entry.run
                if run is not None:
                    run.abort_event.set()
                    run.status = "aborted"
                    run_to_abort = run
                entry.completion_event.set()
            if run_to_abort is not None:
                try:
                    self._abort_run(run_to_abort)
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(
                        f"[workflow] approval timeout cleanup failed attempt={run_to_abort.attempt_id}: {exc!r}\n"
                    )
            raise entry.error

        if isinstance(entry.error, WorkflowApprovalTimeoutError):
            raise entry.error
        if entry.error is not None:
            raise WorkflowApprovalError(f"Approval {approval_id!r} could not start: {entry.error}") from entry.error
        run = entry.run
        if run is None:
            raise WorkflowApprovalError(f"Approval {approval_id!r} has no workflow run")
        if run.error is not None:
            raise WorkflowApprovalError(f"Approval {approval_id!r} crashed: {run.error}") from run.error
        if run.status != "success":
            raise WorkflowApprovalError(f"Approval {approval_id!r} finished with status {run.status!r}")
        value = run.vars.get("APPROVED")
        if value not in {"0", "1"}:
            raise WorkflowApprovalError(f"Approval {approval_id!r} returned invalid APPROVED value")
        if "APPROVED_METADATA" in run.vars:
            try:
                resolved_metadata = json.loads(run.vars["APPROVED_METADATA"])
                result = ApproveResult(approved=int(value), resolved_metadata=resolved_metadata)
            except (ValueError, RecursionError):
                raise WorkflowApprovalError(
                    f"Approval {approval_id!r} returned invalid APPROVED_METADATA; expected a JSON object"
                ) from None
            return result.model_dump(mode="json")
        return int(value)

    @staticmethod
    def _approval_fingerprint(context: AgentExecutionContextPayload, metadata_json: str) -> str:
        payload = json.dumps(context.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(f"{payload}\0{metadata_json}".encode()).hexdigest()

    def snapshot_key_for_task(self, task_id: str) -> str | None:
        """Klíč source snapshotu posledního runu tasku (pro `undo`); None pro pojmenovaná workflow."""

        with self._lock:
            run = self._runs.get(task_id)
            return run.snapshot_key if run is not None else None

    def abort(self, context: AgentExecutionContextPayload) -> dict[str, Any]:
        """Zruší workflow: pošle SIGTERM enginu (ten zruší kroky) nebo uklidí joby podle labelů."""

        namespace = namespace_for_context(context, self.settings)
        with self._lock:
            run = self._runs.get(context.task_id)
            approval_entries = [
                entry
                for entry in self._approval_runs.values()
                if entry.parent_run_id == context.run_id and not entry.completion_event.is_set()
            ]
            for entry in approval_entries:
                entry.cancelled = True
                entry.error = WorkflowApprovalError(f"Approval for run {context.run_id!r} was aborted")
                if entry.run is not None:
                    entry.run.abort_event.set()
                    entry.run.status = "aborted"
                entry.completion_event.set()
            approval_runs = [entry.run for entry in approval_entries if entry.run is not None]
        if run is not None:
            run.abort_event.set()
            run.status = "aborted"

        runs_to_abort = [item for item in [run, *approval_runs] if item is not None]
        if runs_to_abort:
            deleted_results = [self._abort_run(item) for item in runs_to_abort]
        else:
            runtime = (context.adapter.runtime if context.adapter and context.adapter.runtime else "").strip().lower()
            executor = runtime if runtime in {"local", "docker"} else self.settings.workflow_executor
            labels = {"agentis.task_id": self._task_label(context), "agentis.run_id": self._run_label(context)}
            deleted_results = [self._abort_by_labels(executor, namespace, labels)]
        deleted = "; ".join(deleted_results)
        self._emit_adapter_event(
            context,
            kind="workflow_abort",
            status="success",
            event_id=f"workflow_abort:{context.run_id}:{uuid4().hex}",
            message="Workflow was stopped, Jobs were deleted.",
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

    def run_result(self, task_id: str) -> dict[str, Any] | None:
        """Stav posledního runu tasku (status, run adresář, output root, chyba); None, pokud run neexistuje."""
        with self._lock:
            run = self._runs.get(task_id)
        if run is None:
            return None
        return {
            "status": run.status,
            "run_dir": run.run_dir,
            "output_root": run.output_root,
            "executor": run.executor,
            "error": str(run.error) if run.error else None,
        }

    # ------------------------------------------------------------------
    # Preparation and launch
    # ------------------------------------------------------------------

    def _prepare(
        self,
        context: AgentExecutionContextPayload,
        worktree: str,
        prompt: str | None,
        *,
        report_to_agentis: bool = True,
        completion_event: threading.Event | None = None,
        approval: bool = False,
        approval_id: str | None = None,
        approve_metadata: str | None = None,
    ) -> tuple[_WorkflowRun, dict[str, Any]]:
        namespace = namespace_for_context(context, self.settings)
        task_label = self._task_label(context)
        if approval:
            if approval_id is None:
                raise ValueError("approval_id is required for approval workflow")
            approval_key = hashlib.sha256(approval_id.encode()).hexdigest()[:10]
            namespace = f"{namespace[:42].rstrip('-')}-approval-{approval_key}"
            task_label = f"approval-{approval_key}"

        worktree_path = Path(worktree)
        # Hex timestamp s pevnou šířkou: lexikografické řazení názvů jobů odpovídá pořadí spuštění.
        attempt_id = self._next_attempt_id()
        is_project_scope = GitAdapterService.is_project_scope(context)
        workflow_name = self._workflow_name(context)
        workflow_relpath = self._workflow_relpath(workflow_name, is_project_scope)
        self._ensure_workflow_file(worktree_path, workflow_relpath, workflow_name)

        external_run_files = is_project_scope or workflow_name is not None
        if external_run_files:
            run_dir = self.settings.project_run_root / context.run_id / attempt_id
        else:
            run_dir = worktree_path / ".agentis" / "runs" / attempt_id
        output_root = run_dir if external_run_files else worktree_path

        values = self._interpolation_values(context, worktree_path, namespace, run_dir=run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "prompt.md").write_text(prompt if prompt else "", encoding="utf-8")
        (run_dir / "context.json").write_text(
            json.dumps(context.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        labels = {"agentis.task_id": task_label, "agentis.run_id": self._run_label(context)}
        spec = {
            "protocol": engine_cli.PROTOCOL_VERSION,
            "workflow": Path(workflow_relpath).stem,
            # Precedence: projektový soubor, pak workflow zabalené v adapteru (bundled fallback).
            "search_paths": [
                str(worktree_path / Path(workflow_relpath).parent),
                str(self.settings.bundled_workflow_dir),
            ],
            "workdir": str(worktree_path),
            "run_dir": str(run_dir),
            "output_root": str(output_root),
            "executor": self._forced_executor(context),
            "executor_default": self.settings.workflow_executor,
            "namespace": namespace,
            "labels": labels,
            "vars": {name: value for name, value in values.items() if name not in ENGINE_OWNED_TOKENS},
            "env": self._spec_env(context, run_dir, values, approve_metadata),
            # Secret se do spec ani do argv nedostane; engine ho vezme z env procesu.
            "pass_env": ["AGENTIS_SERVICE_TOKEN"],
            "require_vars": ["APPROVED"] if approval else [],
        }
        spec_path = engine_cli.write_spec(run_dir, spec)
        summary = self._check(spec_path)
        executor = str(summary.get("executor", ""))

        snapshot_key: str | None = None
        if not approval and not workflow_name:
            snapshot_key = build_snapshot_key("workflow", context.run_id, context.task_id, attempt_id)
            snapshot_sources_best_effort(worktree_path, snapshot_key, label="workflow-start")
        if report_to_agentis:
            get_status_registry().run_update(
                context.run_id,
                kind="workflow",
                worktree=str(worktree_path),
                workflow=workflow_name,
            )

        run = _WorkflowRun(
            context=context,
            worktree=worktree_path,
            namespace=namespace,
            attempt_id=attempt_id,
            run_dir=run_dir,
            output_root=output_root,
            executor=executor,
            spec_path=spec_path,
            labels=labels,
            task_label=task_label,
            workflow_name=workflow_name,
            workflow_relpath=workflow_relpath,
            approve_metadata=approve_metadata,
            snapshot_key=snapshot_key,
            report_to_agentis=report_to_agentis,
            completion_event=completion_event or threading.Event(),
        )
        return run, {
            "action": "workflow_start",
            "task_id": context.task_id,
            "attempt": attempt_id,
            "namespace": namespace,
            "executor": executor,
            "workflow": workflow_name,
            "workflow_file": workflow_relpath,
            "steps": [str(step.get("name", "")) for step in summary.get("steps", [])],
        }

    def _check(self, spec_path: Path) -> dict[str, Any]:
        completed = engine_cli.check(self.settings.workflow_command, spec_path)
        if completed.returncode == engine_cli.EXIT_BUSY:
            raise WorkflowBusyError(engine_cli.tail(completed.stderr))
        if completed.returncode == engine_cli.EXIT_INVALID:
            raise ValueError(engine_cli.tail(completed.stderr))
        if completed.returncode != engine_cli.EXIT_OK:
            raise engine_cli.EngineCommandError(
                f"agentis-workflow check failed (exit {completed.returncode}): {engine_cli.tail(completed.stderr)}"
            )
        return engine_cli.check_summary(completed)

    def _launch(self, run: _WorkflowRun, *, name: str) -> None:
        """Spustí engine `run --spec` a reader thread; volá se pod `self._lock`, takže jen krátké operace."""

        run.process = engine_cli.spawn_run(self.settings.workflow_command, run.spec_path, self._child_env())
        run.thread = threading.Thread(target=self._thread_main, args=(run,), name=name, daemon=True)
        run.thread.start()

    def _child_env(self) -> dict[str, str]:
        """Env procesu enginu: jediné místo, kudy se service token dostane ven z adapteru (viz `pass_env`)."""

        env = dict(os.environ)
        # Adapterový API token engine nepotřebuje; v env procesu enginu (a jeho podprocesech) nemá být.
        env.pop("AGENTIS_API_TOKEN", None)
        env.pop("AGENTIS_TOKEN", None)
        if self.settings.agentis_service_token:
            env["AGENTIS_SERVICE_TOKEN"] = self.settings.agentis_service_token
        return env

    def _next_attempt_id(self) -> str:
        with self._lock:
            value = max(time.time_ns() // 1_000_000, self._last_attempt_value + 1)
            self._last_attempt_value = value
        return f"{value:011x}"

    def _trim_approval_history_locked(self) -> None:
        if len(self._approval_runs) < APPROVAL_HISTORY_MAX_ENTRIES:
            return
        for key, entry in list(self._approval_runs.items()):
            thread = entry.run.thread if entry.run is not None else None
            if entry.completion_event.is_set() and (thread is None or not thread.is_alive()):
                del self._approval_runs[key]
                if len(self._approval_runs) < APPROVAL_HISTORY_MAX_ENTRIES:
                    return

    def _abort_run(self, run: _WorkflowRun) -> str:
        process = run.process
        if process is not None and process.poll() is None:
            engine_cli.signal_process_group(process, signal.SIGTERM)
            try:
                returncode = process.wait(timeout=ABORT_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                return "stop signal sent; workflow engine is still running"
            return f"workflow engine stopped (exit {returncode})"
        return self._abort_by_labels(run.executor, run.namespace, run.labels)

    def _abort_by_labels(self, executor: str, namespace: str, labels: dict[str, str]) -> str:
        return engine_cli.abort_by_labels(
            self.settings.workflow_command,
            executor=executor,
            namespace=namespace,
            labels=labels,
        )

    def _active_threads(self) -> list[threading.Thread]:
        with self._lock:
            threads = [run.thread for run in self._runs.values() if run.thread is not None]
            threads.extend(
                entry.run.thread
                for entry in self._approval_runs.values()
                if entry.run is not None and entry.run.thread is not None
            )
        return [thread for thread in threads if thread.is_alive()]

    # ------------------------------------------------------------------
    # Spec helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _workflow_name(context: AgentExecutionContextPayload) -> str | None:
        if context.adapter and context.adapter.workflow:
            return context.adapter.workflow
        return None

    @staticmethod
    def _workflow_relpath(workflow_name: str | None, is_project_scope: bool) -> str:
        if workflow_name:
            return f"{WORKFLOW_DIR_RELPATH}/{workflow_name}.yaml"
        name = PROJECT_WORKFLOW_NAME if is_project_scope else DEFAULT_WORKFLOW_NAME
        return f"{WORKFLOW_DIR_RELPATH}/{name}.yaml"

    def _ensure_workflow_file(self, worktree: Path, workflow_relpath: str, workflow_name: str | None) -> None:
        """Včasná chyba s cestami; samotné hledání a validaci dělá engine (`check`)."""

        if (worktree / workflow_relpath).is_file():
            return
        bundled_path = self.settings.bundled_workflow_dir / Path(workflow_relpath).name
        if bundled_path.is_file():
            return
        workflow_path = worktree / workflow_relpath
        if workflow_name:
            raise FileNotFoundError(
                f"Workflow {workflow_name!r} requires file {workflow_relpath} in the project "
                f"({workflow_path}) or the bundled fallback ({bundled_path}), but neither exists"
            )
        raise FileNotFoundError(
            f"Project has no workflow file {workflow_relpath} ({workflow_path}) "
            f"and no bundled fallback ({bundled_path}); the run cannot be started via the workflow runtime"
        )

    def _forced_executor(self, context: AgentExecutionContextPayload) -> str | None:
        """Runtime `local` nebo `docker` vynutí executor bez ohledu na YAML; jinak rozhodne YAML / default."""

        runtime = (context.adapter.runtime if context.adapter and context.adapter.runtime else "").strip().lower()
        return runtime if runtime in {"local", "docker"} else None

    def _spec_env(
        self,
        context: AgentExecutionContextPayload,
        run_dir: Path,
        values: dict[str, str],
        approve_metadata: str | None,
    ) -> dict[str, str]:
        """Nesecretní env kroků. Tokeny (AGENTIS_*_TOKEN) sem nepatří — viz `pass_env`."""

        env = dict(values)
        env.update(task_header_env(context.headers))
        env.update(
            {
                "AGENTIS_RUN_ID": context.run_id,
                "AGENTIS_TASK_ID": context.task_id,
                "AGENTIS_PROJECT_ID": str(context.project_id) if context.project_id is not None else "",
                "AGENTIS_RUN_DIR": str(run_dir),
                "AGENTIS_PROMPT_FILE": str(run_dir / "prompt.md"),
                "AGENTIS_CONTEXT_FILE": str(run_dir / "context.json"),
            }
        )
        if approve_metadata is not None:
            env["AGENTIS_APPROVE_METADATA"] = approve_metadata
        if self.settings.agentis_endpoint:
            env["AGENTIS_ENDPOINT"] = self.settings.agentis_endpoint
        adapter = context.adapter
        if context.session_id:
            env["AGENTIS_SESSION_ID"] = context.session_id
        if adapter and adapter.model:
            env["AGENTIS_MODEL"] = adapter.model
        if adapter and adapter.agent:
            env["AGENTIS_AGENT"] = adapter.agent
        if adapter and adapter.effort:
            env["AGENTIS_EFFORT"] = adapter.effort
        if adapter:
            env["AGENTIS_AUTO_MERGE"] = "true" if adapter.auto_merge else "false"
        return env

    @staticmethod
    def _task_label(context: AgentExecutionContextPayload) -> str:
        return _safe_label(context.task_id) or "task"

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

    # ------------------------------------------------------------------
    # Reader thread: events, result, outputs
    # ------------------------------------------------------------------

    def _thread_main(self, run: _WorkflowRun) -> None:
        try:
            self._consume_process(run)
            self._finish(run)
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.error = exc
            sys.stderr.write(f"[workflow] run {run.context.run_id} crashed: {exc!r}\n")
            if run.report_to_agentis:
                self._emit_adapter_event(
                    run.context,
                    kind="workflow",
                    status="failed",
                    event_id=f"workflow:{run.context.run_id}:{run.attempt_id}",
                    message="Workflow run failed.",
                    data={"error": str(exc)},
                )
        finally:
            if run.report_to_agentis:
                get_status_registry().run_finished(run.context.run_id, run.status)
            run.completion_event.set()

    def _consume_process(self, run: _WorkflowRun) -> None:
        process = run.process
        if process is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("workflow engine process has no pipes")
        stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(run, process),
            name=f"workflow-stderr-{run.attempt_id}",
            daemon=True,
        )
        stderr_thread.start()
        for line in process.stdout:
            self._handle_line(run, line)
        run.returncode = process.wait()
        stderr_thread.join()

    def _drain_stderr(self, run: _WorkflowRun, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            text = line.rstrip("\n")
            if text:
                run.stderr_tail.append(text)
                sys.stderr.write(f"[workflow-engine {run.context.run_id}] {text}\n")

    def _handle_line(self, run: _WorkflowRun, line: str) -> None:
        text = line.strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            event = None
        if not isinstance(event, dict):
            sys.stderr.write(f"[workflow] unparsed engine output for run {run.context.run_id}: {text[:200]}\n")
            return
        try:
            self._handle_event(run, event)
        except Exception as exc:  # noqa: BLE001 - chyba reportingu nesmí zahodit výsledek runu
            sys.stderr.write(f"[workflow] reporting engine event {event.get('type')!r} failed: {exc!r}\n")

    def _handle_event(self, run: _WorkflowRun, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if not run.report_to_agentis:
            return
        event_prefix = f"workflow_step:{run.context.run_id}:{run.attempt_id}"
        if kind == "workflow_started":
            self._emit_adapter_event(
                run.context,
                kind="workflow",
                status="success",
                event_id=f"workflow:{run.context.run_id}:{run.attempt_id}",
                message="Workflow started.",
                data={"attempt": run.attempt_id, "namespace": run.namespace, "executor": run.executor},
            )
        elif kind == "step_started":
            self._emit_adapter_event(
                run.context,
                kind="workflow_step",
                status="started",
                event_id=f"{event_prefix}:{event['index']}",
                message=str(event["step"]),
                data=self._step_data(event),
            )
        elif kind == "step_succeeded":
            self._emit_adapter_event(
                run.context,
                kind="workflow_step",
                status="success",
                event_id=f"{event_prefix}:{event['index']}",
                message=str(event["step"]),
                data=self._step_data(event),
            )
        elif kind == "step_failed":
            error = event.get("error")
            message = f"{event['step']}: {error}" if error else f"Krok selhal ({event.get('result')}): {event['step']}"
            data = self._step_data(event)
            data.update(
                {
                    "result": event.get("result"),
                    "log_tail": event.get("log_tail", ""),
                    "attempts": event.get("attempts"),
                    "continueOnError": event.get("continue_on_error", False),
                }
            )
            if error:
                data["error"] = error
            self._emit_adapter_event(
                run.context,
                kind="workflow_step",
                status="failed",
                event_id=f"{event_prefix}:{event['index']}",
                message=message,
                data=data,
            )
        elif kind == "step_skipped":
            data = {"step": event["step"], "step_index": event["index"], "needs": event.get("needs", []), "skipped": True}
            if event.get("reason") == "condition":
                data["condition"] = event.get("condition")
                data["vars"] = event.get("vars", {})
                message = f"Step skipped (if: {event.get('condition')}): {event['step']}"
            else:
                data["failed_step"] = event.get("failed_step")
                message = f"Step skipped (workflow failed): {event['step']}"
            self._emit_adapter_event(
                run.context,
                kind="workflow_step",
                status="skipped",
                event_id=f"{event_prefix}:{event['index']}",
                message=message,
                data=data,
            )
        elif kind == "warning":
            sys.stderr.write(f"[workflow] {run.context.run_id} warning: {event.get('message')}\n")

    @staticmethod
    def _step_data(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "step": event["step"],
            "step_index": event["index"],
            "needs": event.get("needs", []),
            "job": event.get("job"),
        }

    def _finish(self, run: _WorkflowRun) -> None:
        result = engine_cli.read_result(run.run_dir)
        if result is None:
            if run.abort_event.is_set():
                run.status = "aborted"
                return
            raise RuntimeError(
                f"workflow engine exited with code {run.returncode} without a valid result.json: "
                f"{engine_cli.tail('\n'.join(run.stderr_tail))}"
            )
        run.vars = {str(name): str(value) for name, value in (result.get("vars") or {}).items()}
        if run.abort_event.is_set() or result.get("status") == "aborted":
            run.status = "aborted"
            return
        run.status = "success" if result.get("status") == "success" else "failed"
        if not run.report_to_agentis:
            return

        # Outputs se aplikují i po selhání workflow — `always` krok tak může doručit failure komentář.
        self._apply_outputs(run, result)
        workflow_event_id = f"workflow:{run.context.run_id}:{run.attempt_id}"
        if run.status == "failed":
            self._emit_adapter_event(
                run.context,
                kind="idle",
                status="failed",
                event_id=workflow_event_id,
                message="Workflow selhalo.",
                data={"failed_step": result.get("failed_step"), "attempt": run.attempt_id},
            )
        else:
            self._emit_adapter_event(
                run.context,
                kind="idle",
                status="success",
                event_id=workflow_event_id,
                message="Workflow finished.",
                data={"attempt": run.attempt_id},
            )

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------

    def _apply_outputs(self, run: _WorkflowRun, result: dict[str, Any]) -> None:
        outputs = result.get("outputs") or {}
        comments: list[dict[str, Any]] = [
            {
                "body": str(comment.get("body", "")),
                "status": comment.get("status"),
                "author_name": comment.get("author_name"),
            }
            for comment in outputs.get("comments") or []
        ]

        attachments: list[dict[str, Any]] = []
        ide = (run.context.ide or "").strip()
        if ide and run.snapshot_key and not GitAdapterService.is_project_scope(run.context):
            attachments.append(
                {
                    "label": "Directory",
                    "value": ide.replace("[%WORKDIR%]", str(run.worktree)),
                    "type": "url",
                }
            )
        attachments.extend(
            {"label": item.get("label"), "value": item.get("value"), "type": item.get("type")}
            for item in outputs.get("attachments") or []
        )
        artifacts = self._collect_artifacts(run, outputs.get("artifacts") or [])

        if run.snapshot_key:
            diff_result = write_changes_diff_best_effort(run.worktree, run.snapshot_key, label="workflow-finish")
            diff_attachment = changes_diff_attachment(diff_result)
            if diff_attachment:
                attachments.append(diff_attachment)

        session_id = outputs.get("session_id")
        if session_id:
            run.context.session_id = session_id
            self._agentis_call(
                method="run.store_session_id",
                params={"run_id": run.context.run_id, "session_id": session_id},
            )

        if comments:
            # Followup akce počítá engine (`if` nad env a `var` outputs). U failure komentáře se nenabízí
            # vůbec — merge/close rozdělané práce po selhaném runu nedává smysl.
            actions: list[dict[str, Any]] = []
            if run.status != "failed":
                actions = [
                    {
                        "title": followup["title"],
                        "prompt": followup.get("prompt", ""),
                        "adapter_method": "start",
                        "workflow": followup["workflow"],
                        "continue_previous_run": followup.get("continue_previous_run", False),
                    }
                    for followup in result.get("followups") or []
                ]
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
                if include_run_outputs and images:
                    clear_screenshot_images(run.worktree)
        elif attachments or artifacts:
            self._emit_adapter_event(
                run.context,
                kind="workflow_outputs",
                status="success",
                event_id=f"workflow_outputs:{run.context.run_id}:{run.attempt_id}",
                message="Workflow outputs were processed.",
                data={"attachments": attachments, "artifact_names": [item.get("name") for item in artifacts]},
            )

    def _collect_artifacts(self, run: _WorkflowRun, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Čte artefakty, které engine zkopíroval do `artifact-staging` v run dir (cesta relativně k run dir)."""

        root = run.run_dir.resolve()
        artifacts: list[dict[str, Any]] = []
        total_bytes = 0
        for item in items[:ARTIFACT_OUTPUT_MAX_FILES]:
            path = (run.run_dir / str(item.get("path", ""))).resolve()
            if root not in path.parents or not path.is_file():
                continue
            try:
                size = path.stat().st_size
                if total_bytes + size > ARTIFACT_OUTPUT_MAX_BYTES:
                    continue
                content = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                continue
            total_bytes += size
            artifacts.append({"name": item.get("name"), "filename": item.get("filename"), "content": content})
        return artifacts

    # ------------------------------------------------------------------
    # Agentis RPC
    # ------------------------------------------------------------------

    def _agentis_call(self, method: str, params: dict[str, Any]) -> None:
        if self._agentis_sink is not None:
            self._agentis_sink(method, params)
            return
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


def _safe_label(name: str) -> str:
    """Stejná normalizace jako labely jobů v enginu, aby busy/abort selektory seděly."""

    ascii_value = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    sanitized = re.sub(r"[^a-z0-9-]+", "-", ascii_value.lower().strip())
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-")
    return sanitized[:20].strip("-") or "step"


__all__ = ["WorkflowApprovalError", "WorkflowApprovalTimeoutError", "WorkflowBusyError", "WorkflowManager"]
