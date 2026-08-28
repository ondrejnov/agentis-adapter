"""Lokální executor pro workflow režim.

Spouští workflow kroky jako bash subprocessy přímo na hostu nad worktree —
protějšek :class:`~common.workflow.runtime.KubectlJobRunner` bez Kubernetes.
Kubernetes-specifická pole workflow YAML (`image`, `mounts`,
`imagePullSecrets`, `resources`) se ignorují; kroky běží pod uživatelem
adapter procesu bez izolace, se stejným bash wrapperem (`set -euo pipefail`,
`cd` do workingDir kroku, jinak `"$WORKDIR"`) jako v Kubernetes.

Bash je potřeba i na Windows (wrapper je bash syntaxe) — hledá se v PATH
(`shutil.which`), takže funguje přes Git Bash nebo WSL; spawn/kill se větví
podle platformy (`start_new_session`+`killpg` na POSIX, `CREATE_NEW_PROCESS_GROUP`
+`taskkill /T` na Windows).
"""

from __future__ import annotations

import os
import shutil
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
_SCRUBBED_HOST_ENV = frozenset({"AGENTIS_TOKEN", "AGENTIS_API_TOKEN", "AGENTIS_SERVICE_TOKEN"})

#: Po SIGTERM dostane process group tolik sekund na úklid, pak přijde SIGKILL.
_KILL_GRACE_SEC = 5.0

#: Windows nemá POSIX process groups ani /bin/bash — spawn/kill se větví podle toho.
_IS_WINDOWS = os.name == "nt"


def _resolve_bash() -> str | None:
    """Najde bash executable; POSIX `/bin/bash`, jinak z PATH (Git Bash/WSL). None = chybí."""

    return shutil.which("bash") or ("/bin/bash" if not _IS_WINDOWS and Path("/bin/bash").exists() else None)


def _spawn_kwargs() -> dict[str, object]:
    """Platform-specific kwargs pro `Popen`, aby šla zabít celá process group / strom."""

    if _IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


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

        bash = _resolve_bash()
        if bash is None:
            return self._spawn_failed(
                name,
                log_path,
                "bash nenalezen v PATH (na Windows nainstaluj Git Bash nebo WSL a přidej ho do PATH)",
            )

        wrapper = build_bash_wrapper(step.run, workdir=step.workingDir or spec.workingDir)
        try:
            with log_path.open("wb") as log_file:
                process = subprocess.Popen(
                    [bash, "-lc", wrapper],
                    cwd=working_dir,
                    env=merged_env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    **_spawn_kwargs(),
                )
        except OSError as exc:
            return self._spawn_failed(name, log_path, str(exc))

        with self._lock:
            self._processes.setdefault(task_label, {})[process.pid] = process
        try:
            status = self._wait(process, timeout=timeout, abort_event=abort_event)
        finally:
            with self._lock:
                self._processes.get(task_label, {}).pop(process.pid, None)

        log_tail = "" if status in {"succeeded", "aborted"} else self._log_tail(log_path)
        if status not in {"succeeded", "aborted"}:
            detail = f": {log_tail}" if log_tail else ""
            sys.stderr.write(f"[workflow] krok '{name}' selhal ({status}, log {log_path}){detail}\n")
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

    def delete_namespace(self, namespace: str) -> None:
        """Lokální executor žádné namespacy nevytváří — mazání je no-op."""

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _spawn_failed(name: str, log_path: Path, reason: str) -> StepResult:
        """Spawn kroku selhal (chybí bash / OSError) — zaloguj do stderru a vrať `failed`.

        Tahle větva běží *před* spuštěním kroku, takže log soubor zůstane prázdný;
        bez tohohle stderr zápisu by selhání (typicky chybějící bash na Windows) nikde
        nebylo vidět, jen v `log_tail` Agentis eventu.
        """

        log_tail = f"(spawn failed: {reason})"
        sys.stderr.write(f"[workflow] krok '{name}' selhal (spawn, log {log_path}): {log_tail}\n")
        return StepResult(status="failed", log_tail=log_tail)

    @staticmethod
    def _ignored_fields(workflow: WorkflowFile) -> list[str]:
        spec = workflow.workflow
        ignored: list[str] = []
        if spec.context:
            ignored.append("context")
        if spec.image:
            ignored.append("image")
        if spec.imagePullSecrets:
            ignored.append("imagePullSecrets")
        if spec.mounts:
            ignored.append("mounts")
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

    @classmethod
    def _kill(cls, process: subprocess.Popen[bytes]) -> None:
        # Celý strom/process group: kroky typicky spouští další procesy (agent, git, ...).
        if process.poll() is not None:
            return
        if _IS_WINDOWS:
            cls._kill_windows(process)
            return
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
    def _kill_windows(process: subprocess.Popen[bytes]) -> None:
        # `taskkill /T` zabije bash i jeho potomky; CREATE_NEW_PROCESS_GROUP nestačí (jen Ctrl+Break).
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_KILL_GRACE_SEC,
            )
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_KILL_GRACE_SEC)
        if process.poll() is None:
            with suppress(OSError):
                process.kill()
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
