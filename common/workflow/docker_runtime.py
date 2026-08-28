"""Docker executor for workflow steps.

Each step runs in a short-lived container through the native Docker CLI. The
workflow manager still owns ordering, retries, timeouts, outputs, and events;
this runner only manages one container's lifecycle.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from common.config import Settings
from common.workflow.runtime import LOG_TAIL_LINES, StepResult, WORKFLOW_LABEL, build_bash_wrapper
from common.workflow.schema import WorkflowFile, WorkflowMount, WorkflowStep

_DOCKER_COMMAND_TIMEOUT_SEC = 60.0
_CLIENT_STOP_GRACE_SEC = 5.0
_HOST_PATH_TYPES = frozenset(
    {"", "DirectoryOrCreate", "Directory", "FileOrCreate", "File", "Socket", "CharDevice", "BlockDevice"}
)
_DOCKER_CLIENT_ENV = frozenset(
    {
        "HOME",
        "PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class DockerContainerRunner:
    """Run workflow steps as native Docker containers."""

    def __init__(self, settings: Settings, *, poll_interval: float = 0.2) -> None:
        self.settings = settings
        self.poll_interval = poll_interval

    def prepare(self, workflow: WorkflowFile, *, namespace: str, run_dir: Path) -> None:
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        for mount in workflow.workflow.mounts:
            self._prepare_host_mount(mount)

        ignored: list[str] = []
        if workflow.workflow.context:
            ignored.append("context")
        if workflow.workflow.imagePullSecrets:
            ignored.append("imagePullSecrets (Docker uses the host credential store)")
        if any(step.resources for step in workflow.workflow.steps):
            ignored.append("steps[].resources")
        if ignored:
            sys.stderr.write(f"[workflow] docker executor ignores fields: {', '.join(ignored)}\n")

    def has_active_run(self, namespace: str, task_label: str) -> bool:
        args = [
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label={WORKFLOW_LABEL}=true",
            "--filter",
            f"label=agentis.task_id={task_label}",
        ]
        for status in ("created", "running", "restarting", "paused"):
            args.extend(("--filter", f"status={status}"))
        output = self._run_cli(*args)
        return bool(output.strip())

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
        image = step.image or spec.image
        if not image:
            return self._spawn_failed(name, run_dir / "logs" / f"{name}.log", "container image is not set")

        merged_env = {**spec.env, **env, **step.env}
        command = self._container_command(workflow, step, name=name, labels=labels, env=merged_env, image=image)
        process_env = os.environ.copy()
        process_env.update(
            {key: value for key, value in merged_env.items() if not self._is_docker_client_env(key)}
        )
        log_path = run_dir / "logs" / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with log_path.open("wb") as log_file:
                process = subprocess.Popen(
                    command,
                    env=process_env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
        except OSError as exc:
            return self._spawn_failed(name, log_path, str(exc))

        status = self._wait(process, name=name, timeout=timeout, abort_event=abort_event)
        log_tail = "" if status in {"succeeded", "aborted"} else self._log_tail(log_path)
        if status not in {"succeeded", "aborted"}:
            detail = f": {log_tail}" if log_tail else ""
            sys.stderr.write(f"[workflow] step '{name}' failed ({status}, log {log_path}){detail}\n")
        return StepResult(status=status, log_tail=log_tail)

    def abort(self, namespace: str, labels: dict[str, str]) -> str:
        args: list[str] = ["ps", "--all", "--quiet"]
        for key, value in labels.items():
            args.extend(("--filter", f"label={key}={value}"))
        container_ids = self._run_cli(*args).split()
        if not container_ids:
            return "removed 0 container(s)"
        removed = sum(self._remove_container(container_id) for container_id in container_ids)
        return f"removed {removed} container(s)"

    def delete_namespace(self, namespace: str) -> None:
        """Docker has no namespaces; workflow cleanup is handled by ``--rm``."""

    def _container_command(
        self,
        workflow: WorkflowFile,
        step: WorkflowStep,
        *,
        name: str,
        labels: dict[str, str],
        env: dict[str, str],
        image: str,
    ) -> list[str]:
        spec = workflow.workflow
        if step.run is None:
            raise ValueError(f"Docker workflow step {step.name!r} has no run script")
        working_dir = step.workingDir or spec.workingDir or env.get("WORKDIR")
        command = [self.settings.docker_command, "run", "--rm", "--init", "--name", name]
        for key, value in labels.items():
            command.extend(("--label", f"{key}={value}"))
        for key, value in env.items():
            # Key-only arguments keep secrets out of argv. Docker control variables
            # retain their host value for the client and are set explicitly only in the container.
            container_env = f"{key}={value}" if self._is_docker_client_env(key) else key
            command.extend(("--env", container_env))
        for source, target, read_only in self._bind_mounts(workflow, env):
            mount = f"type=bind,source={source},target={target}"
            if read_only:
                mount += ",readonly"
            command.extend(("--mount", mount))
        if working_dir:
            command.extend(("--workdir", working_dir))
        command.extend(("--entrypoint", "/bin/bash"))
        command.extend(
            (
                image,
                "-lc",
                build_bash_wrapper(step.run, workdir=working_dir, workdir_env="WORKDIR"),
            )
        )
        return command

    def _bind_mounts(self, workflow: WorkflowFile, env: dict[str, str]) -> list[tuple[str, str, bool]]:
        mounts = [self._host_mount(mount) for mount in workflow.workflow.mounts]
        for env_name in ("WORKDIR", "AGENTIS_RUN_DIR", "MAIN_DIR"):
            value = env.get(env_name)
            if not value:
                continue
            path = Path(value)
            if not path.is_absolute() or not path.exists() or self._path_is_mounted(path, mounts):
                continue
            mounts.append((str(path), str(path), False))
        return mounts

    @staticmethod
    def _path_is_mounted(path: Path, mounts: list[tuple[str, str, bool]]) -> bool:
        for source, target, _read_only in mounts:
            try:
                relative = path.relative_to(Path(target))
            except ValueError:
                continue
            if Path(source) / relative == path:
                return True
        return False

    @staticmethod
    def _host_mount(mount: WorkflowMount) -> tuple[str, str, bool]:
        if not Path(mount.mountPath).is_absolute():
            raise ValueError(f"Docker mount {mount.name!r} mountPath must be absolute")
        if mount.mountPropagation:
            raise ValueError(f"Docker mount {mount.name!r} does not support mountPropagation")
        if mount.subPathExpr:
            raise ValueError(f"Docker mount {mount.name!r} does not support subPathExpr")
        source_config = mount.volume_source()
        host_path = source_config.get("hostPath")
        if not isinstance(host_path, dict) or not isinstance(host_path.get("path"), str):
            raise ValueError(f"Docker mount {mount.name!r} requires a hostPath volume source")
        source = Path(host_path["path"])
        if mount.subPath:
            sub_path = Path(mount.subPath)
            if sub_path.is_absolute() or ".." in sub_path.parts:
                raise ValueError(f"Docker mount {mount.name!r} subPath must stay within hostPath")
            source /= sub_path
        if not source.is_absolute():
            raise ValueError(f"Docker mount {mount.name!r} hostPath must be absolute")
        return str(source), mount.mountPath, bool(mount.readOnly)

    @classmethod
    def _prepare_host_mount(cls, mount: WorkflowMount) -> None:
        source, _target, _read_only = cls._host_mount(mount)
        source_path = Path(source)
        host_path = mount.volume_source()["hostPath"]
        path_type = host_path.get("type") or ""
        if path_type not in _HOST_PATH_TYPES:
            raise ValueError(f"Docker mount {mount.name!r} has unsupported hostPath type {path_type!r}")
        if path_type == "DirectoryOrCreate":
            source_path.mkdir(parents=True, exist_ok=True)
        elif path_type == "FileOrCreate":
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.touch(exist_ok=True)
        elif not source_path.exists():
            raise ValueError(f"Docker mount {mount.name!r} source does not exist: {source_path}")
        elif path_type == "Directory" and not source_path.is_dir():
            raise ValueError(f"Docker mount {mount.name!r} source is not a directory: {source_path}")
        elif path_type == "File" and not source_path.is_file():
            raise ValueError(f"Docker mount {mount.name!r} source is not a file: {source_path}")
        elif path_type == "Socket" and not stat.S_ISSOCK(source_path.stat().st_mode):
            raise ValueError(f"Docker mount {mount.name!r} source is not a socket: {source_path}")
        elif path_type == "CharDevice" and not stat.S_ISCHR(source_path.stat().st_mode):
            raise ValueError(f"Docker mount {mount.name!r} source is not a character device: {source_path}")
        elif path_type == "BlockDevice" and not stat.S_ISBLK(source_path.stat().st_mode):
            raise ValueError(f"Docker mount {mount.name!r} source is not a block device: {source_path}")

    def _wait(
        self,
        process: subprocess.Popen[bytes],
        *,
        name: str,
        timeout: float,
        abort_event: threading.Event,
    ) -> str:
        deadline = time.monotonic() + timeout
        while True:
            if abort_event.is_set():
                self._stop_client(process)
                self._remove_container(name)
                return "aborted"
            if process.poll() is not None:
                return "succeeded" if process.returncode == 0 else "failed"
            if time.monotonic() >= deadline:
                self._stop_client(process)
                self._remove_container(name)
                return "timeout"
            time.sleep(self.poll_interval)

    def _remove_container(self, name: str) -> bool:
        try:
            self._run_cli("rm", "--force", name)
        except RuntimeError as exc:
            sys.stderr.write(f"[workflow] failed to remove Docker container {name}: {exc}\n")
            return False
        return True

    @staticmethod
    def _stop_client(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=_CLIENT_STOP_GRACE_SEC)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=_CLIENT_STOP_GRACE_SEC)
            except subprocess.TimeoutExpired:
                pass

    def _run_cli(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                [self.settings.docker_command, *args],
                capture_output=True,
                text=True,
                timeout=_DOCKER_COMMAND_TIMEOUT_SEC,
                check=False,
            )
        except OSError as exc:
            raise RuntimeError(f"Docker command failed to start: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Docker command timed out after {_DOCKER_COMMAND_TIMEOUT_SEC:g}s") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown Docker error"
            raise RuntimeError(f"docker {' '.join(args)} failed: {detail}")
        return completed.stdout

    @staticmethod
    def _is_docker_client_env(name: str) -> bool:
        return name in _DOCKER_CLIENT_ENV or name.startswith("DOCKER_")

    @staticmethod
    def _spawn_failed(name: str, log_path: Path, reason: str) -> StepResult:
        log_tail = f"(spawn failed: {reason})"
        sys.stderr.write(f"[workflow] step '{name}' failed (spawn, log {log_path}): {log_tail}\n")
        return StepResult(status="failed", log_tail=log_tail)

    @staticmethod
    def _log_tail(log_path: Path, *, lines: int = LOG_TAIL_LINES) -> str:
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"(log unavailable: {exc})"
        return "\n".join(content.splitlines()[-lines:]).strip()


__all__ = ["DockerContainerRunner"]
