"""Lokální executor pro workflow režim.

Spouští workflow kroky jako bash subprocessy přímo na hostu nad worktree —
protějšek :class:`~common.workflow.runtime.KubectlJobRunner` bez Kubernetes.
Kubernetes-specifická pole workflow YAML (`image`, `volumes`, `volumeMounts`,
`imagePullSecrets`, `resources`) se ignorují; kroky běží pod uživatelem
adapter procesu bez izolace, se stejným bash wrapperem (`set -euo pipefail`,
sourcing `envFiles`, `cd` do workingDir kroku, jinak `"$WORKDIR"`) jako v Kubernetes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

from common.config import Settings
from common.workflow.runtime import LOG_TAIL_LINES, StepResult, build_bash_wrapper
from common.workflow.schema import WorkflowFile, WorkflowStep

#: Proměnné adapter procesu, které se do prostředí lokálních kroků nesmí propsat.
_SCRUBBED_HOST_ENV = frozenset({"AGENTIS_TOKEN"})

#: Po SIGTERM dostane process group tolik sekund na úklid, pak přijde SIGKILL.
_KILL_GRACE_SEC = 5.0


class LocalProcessRunner:
    """Workflow kroky jako lokální procesy; implementuje `WorkflowStepRunner`."""

    def __init__(self, settings: Settings, *, poll_interval: float = 0.2) -> None:
        self.settings = settings
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        #: Běžící procesy per task label — pro busy-check a abort.
        self._processes: dict[str, dict[int, subprocess.Popen[bytes]]] = {}

    # ------------------------------------------------------------------
    # WorkflowStepRunner protokol
    # ------------------------------------------------------------------

    def prepare(self, workflow: WorkflowFile, *, namespace: str, run_dir: Path) -> None:
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        ignored = self._ignored_fields(workflow)
        if ignored:
            sys.stderr.write(f"[workflow] local executor ignoruje Kubernetes pole: {', '.join(ignored)}\n")

    def has_active_run(self, namespace: str, task_label: str) -> bool:
        with self._lock:
            processes = self._processes.get(task_label, {})
            return any(process.poll() is None for process in processes.values())

    def run_step(
        self,
        workflow: WorkflowFile,
        step: WorkflowStep,
        *,
        namespace: str,
        name: str,
        labels: dict[str, str],
        env: dict[str, str],
        timeout: float,
        abort_event: threading.Event,
        run_dir: Path,
    ) -> StepResult:
        spec = workflow.workflow
        host_env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED_HOST_ENV}
        merged_env = {**host_env, **spec.env, **env, **step.env}
        working_dir = step.workingDir or spec.workingDir or merged_env.get("WORKDIR") or str(run_dir)
        task_label = labels.get("agentis.task_id", "task")
        log_path = run_dir / "logs" / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with log_path.open("wb") as log_file:
                process = subprocess.Popen(
                    ["/bin/bash", "-lc", build_bash_wrapper(spec.envFiles, step.run, workdir=step.workingDir or spec.workingDir)],
                    cwd=working_dir,
                    env=merged_env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            return StepResult(status="failed", log_tail=f"(spawn failed: {exc})")

        with self._lock:
            self._processes.setdefault(task_label, {})[process.pid] = process
        try:
            status = self._wait(process, timeout=timeout, abort_event=abort_event)
        finally:
            with self._lock:
                self._processes.get(task_label, {}).pop(process.pid, None)

        log_tail = "" if status in {"succeeded", "aborted"} else self._log_tail(log_path)
        return StepResult(status=status, log_tail=log_tail)

    def abort(self, namespace: str, labels: dict[str, str]) -> str:
        task_label = labels.get("agentis.task_id", "")
        with self._lock:
            processes = list(self._processes.get(task_label, {}).values())
        killed = 0
        for process in processes:
            if process.poll() is None:
                self._kill(process)
                killed += 1
        return f"killed {killed} process(es)"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _ignored_fields(workflow: WorkflowFile) -> list[str]:
        spec = workflow.workflow
        ignored: list[str] = []
        if spec.image:
            ignored.append("image")
        if spec.imagePullSecrets:
            ignored.append("imagePullSecrets")
        if spec.volumeMounts:
            ignored.append("volumeMounts")
        if workflow.volumes:
            ignored.append("volumes")
        if any(step.image for step in spec.steps):
            ignored.append("steps[].image")
        if any(step.resources for step in spec.steps):
            ignored.append("steps[].resources")
        return ignored

    def _wait(self, process: subprocess.Popen[bytes], *, timeout: float, abort_event: threading.Event) -> str:
        """Sleduje proces do dokončení; vrací `succeeded` / `failed` / `timeout` / `aborted`."""

        deadline = time.monotonic() + timeout
        while True:
            if abort_event.is_set():
                self._kill(process)
                return "aborted"
            if process.poll() is not None:
                return "succeeded" if process.returncode == 0 else "failed"
            if time.monotonic() >= deadline:
                self._kill(process)
                return "timeout"
            time.sleep(self.poll_interval)

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
        # Celou process group: kroky typicky spouští další procesy (agent, git, ...).
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=_KILL_GRACE_SEC)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_KILL_GRACE_SEC)

    @staticmethod
    def _log_tail(log_path: Path, *, lines: int = LOG_TAIL_LINES) -> str:
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"(log unavailable: {exc})"
        return "\n".join(content.splitlines()[-lines:]).strip()


__all__ = ["LocalProcessRunner"]
