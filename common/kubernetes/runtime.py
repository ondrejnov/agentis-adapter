"""Kubernetes deploy machinery used by the adapters.

``KubernetesRuntime`` is a plain collaborator (NOT an adapter): it owns the
Kubernetes-specific concerns — namespace/ingress naming, manifest resolution and
``apply``/``delete``, the optional local ``opencode web`` runtime and the
readiness probe. Both the local CLI adapter (in ``kubernetes`` mode) and the
``KubernetesAdapterService`` compose it; neither borrows it from the other, so
the adapter inheritance tree stays free of Kubernetes wiring.

The git/worktree layer resolves the workspace path and passes it in; this helper
never runs git itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, TextIO

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.adapter_base import log_json
from common.kubernetes.deploy_config import load_deploy_config
from common.kubernetes.manifest_parser import OpenCodeManifestParser
from common.local_setup import build_local_setup_shell_command


@dataclass
class LocalOpenCodeRuntime:
    process: subprocess.Popen[str]
    url: str
    workspace_path: str


class KubernetesRuntime:
    """Deploy/teardown collaborator for the Kubernetes-backed OpenCode runtime."""

    DEFAULT_MANIFEST_NAME = "opencode.yaml"
    PROJECT_MANIFEST_NAME = "opencode-project.yaml"
    AGENTIS_CONFIG_NAME = "opencode.json"
    AGENTIS_CONFIG_TEMPLATE = Path(__file__).resolve().parents[2] / "opencode.json.tpl"
    LOCAL_RUNTIME = "local"
    _local_runtimes: dict[str, LocalOpenCodeRuntime] = {}

    def __init__(
        self,
        context: AgentExecutionContextPayload,
        settings: Settings,
        workspace_path: Path,
    ) -> None:
        self.context = context
        self.settings = settings
        self.workspace_path = Path(workspace_path)

    # ------------------------------------------------------------------
    # Scope / namespace / URL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def is_project_scope(context: AgentExecutionContextPayload) -> bool:
        return bool(context.adapter and context.adapter.scope == "project")

    @staticmethod
    def _kubernetes_safe_name(value: str) -> str:
        import unicodedata

        ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        sanitized = re.sub(r"[^a-z0-9-]+", "-", ascii_value.lower().strip())
        return re.sub(r"-{2,}", "-", sanitized).strip("-")

    @classmethod
    def namespace_for_context(cls, context: AgentExecutionContextPayload, settings: Settings) -> str:
        if context.namespace and context.namespace.strip():
            return context.namespace.strip()
        if cls.is_project_scope(context):
            project_name = cls._kubernetes_safe_name(context.project_slug or context.project_title or "")
            if not project_name:
                raise RuntimeError("project_slug cannot be converted to a Kubernetes namespace")
            namespace = f"project-{project_name}"
            return namespace[:63].strip("-")
        if context.task_number is None:
            namespace = cls._kubernetes_safe_name(context.task_id)
            if not namespace:
                raise RuntimeError("task_id cannot be converted to a Kubernetes namespace")
            return namespace

        prefix = cls._kubernetes_safe_name(settings.namespace_prefix)
        title = cls._kubernetes_safe_name(context.title[:20])
        namespace = "-".join(part for part in (prefix, str(context.task_number), title) if part)
        if not namespace:
            raise RuntimeError("namespace cannot be empty")
        return namespace[:63].strip("-")

    @staticmethod
    def _ingress_host(namespace: str, *, prefix: str | None = None) -> str:
        domain_suffix = f".{OpenCodeManifestParser.INGRESS_DOMAIN_SUFFIX}"
        if prefix:
            return f"{prefix}-{namespace}{domain_suffix}"
        return f"{namespace}{domain_suffix}"

    @classmethod
    def opencode_url_for_context(cls, context: AgentExecutionContextPayload, settings: Settings) -> str:
        app_host_raw = context.app_host or settings.app_host
        if app_host_raw:
            stripped = app_host_raw.strip()
            domain_suffix = f".{OpenCodeManifestParser.INGRESS_DOMAIN_SUFFIX}"
            host = stripped if stripped.endswith(domain_suffix) else f"{stripped}{domain_suffix}"
            return f"http://{host}"

        namespace = cls.namespace_for_context(context, settings)
        return f"http://{cls._ingress_host(namespace)}"

    @classmethod
    def dev_server_url_for_context(cls, context: AgentExecutionContextPayload, settings: Settings) -> str:
        namespace = cls.namespace_for_context(context, settings)
        return f"http://{cls._ingress_host(namespace, prefix='app')}"

    # ------------------------------------------------------------------
    # Deploy-config / manifest resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_directory(path: Path) -> bool:
        if path.exists():
            return path.is_dir()
        return path.suffix == ""

    @staticmethod
    def _runtime_key(context: AgentExecutionContextPayload) -> str:
        return context.run_id or context.task_id

    def _is_local_runtime(self) -> bool:
        return bool(self.context.adapter and self.context.adapter.runtime == self.LOCAL_RUNTIME)

    def _deploy_config_roots(self) -> list[Path]:
        roots = [self.workspace_path]
        if not self.is_project_scope(self.context):
            source_root = Path(self.context.working_dir)
            if source_root != self.workspace_path:
                roots.append(source_root)
        return roots

    def _load_deploy_config_for_scope(self, scope: str):
        for deploy_config_root in self._deploy_config_roots():
            deploy_config = load_deploy_config(deploy_config_root, scope=scope)
            if deploy_config is not None:
                return deploy_config
        return None

    def _should_use_local_opencode(self) -> bool:
        return self._is_local_runtime() and self._load_deploy_config_for_scope(self.LOCAL_RUNTIME) is None

    def _resolve_manifest_source(self) -> Path:
        if self._is_local_runtime():
            deploy_config = self._load_deploy_config_for_scope(self.LOCAL_RUNTIME)
            if deploy_config is not None:
                return deploy_config.manifest_path

        deploy_scope = "project" if self.is_project_scope(self.context) else "worktree"
        deploy_config = self._load_deploy_config_for_scope(deploy_scope)
        if deploy_config is not None:
            return deploy_config.manifest_path

        configured_path = self.settings.manifest_path
        if self.is_project_scope(self.context):
            base_directory = configured_path if self._looks_like_directory(configured_path) else configured_path.parent
            return base_directory / self.PROJECT_MANIFEST_NAME

        manifest_name = self.context.adapter.manifest if self.context.adapter else None

        if not manifest_name:
            if self._looks_like_directory(configured_path):
                return configured_path / self.DEFAULT_MANIFEST_NAME
            return configured_path

        base_directory = configured_path if self._looks_like_directory(configured_path) else configured_path.parent
        return base_directory / manifest_name

    # ------------------------------------------------------------------
    # Local `opencode web` runtime
    # ------------------------------------------------------------------

    def _opencode_url(self) -> str:
        if self._is_local_runtime():
            runtime = self._local_runtimes.get(self._runtime_key(self.context))
            if runtime is not None and runtime.process.poll() is None:
                return runtime.url
        return self.opencode_url_for_context(self.context, self.settings)

    @staticmethod
    def _extract_local_opencode_url(line: str) -> str | None:
        match = re.search(r"https?://(?:0\.0\.0\.0|127\.0\.0\.1|localhost):(?P<port>\d+)", line)
        if not match:
            match = re.search(r"(?:^|\s)(?:0\.0\.0\.0|127\.0\.0\.1|localhost):(?P<port>\d+)", line)
        if not match:
            return None
        return f"http://127.0.0.1:{match.group('port')}"

    @staticmethod
    def _read_local_opencode_output(stream: TextIO | None, output_queue: queue.Queue[str]) -> None:
        if stream is None:
            return
        for line in stream:
            sys.stderr.write(f"[opencode-local] {line}")
            sys.stderr.flush()
            output_queue.put(line)

    def _spawn_local_opencode(self, workspace_path: Path, timeout: float = 30.0) -> LocalOpenCodeRuntime:
        runtime_key = self._runtime_key(self.context)
        existing = self._local_runtimes.get(runtime_key)
        if existing is not None and existing.process.poll() is None:
            return existing

        env = os.environ.copy()
        if self.settings.public_base_url:
            env["AGENTIS_URL"] = self.settings.public_base_url

        process = subprocess.Popen(
            ["bash", "-c", build_local_setup_shell_command(["opencode", "web", "--hostname", "0.0.0.0"])],
            cwd=str(workspace_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        assert process.stdout is not None
        output_queue: queue.Queue[str] = queue.Queue()
        threading.Thread(
            target=self._read_local_opencode_output,
            args=(process.stdout, output_queue),
            daemon=True,
        ).start()

        deadline = time.monotonic() + timeout
        url: str | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"opencode web exited before reporting URL with code {process.returncode}")

            try:
                line = output_queue.get(timeout=0.1)
            except queue.Empty:
                time.sleep(0.1)
                continue

            url = self._extract_local_opencode_url(line)
            if url is not None:
                break

        if url is None:
            process.terminate()
            raise TimeoutError("opencode web did not report a listening port")

        runtime = LocalOpenCodeRuntime(process=process, url=url, workspace_path=str(workspace_path))
        self._local_runtimes[runtime_key] = runtime
        return runtime

    def _stop_local_opencode(self) -> bool:
        runtime = self._local_runtimes.pop(self._runtime_key(self.context), None)
        if runtime is None:
            return False
        if runtime.process.poll() is not None:
            return False

        runtime.process.terminate()
        try:
            runtime.process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            runtime.process.kill()
            runtime.process.wait(timeout=10.0)
        return True

    # ------------------------------------------------------------------
    # kubectl helpers
    # ------------------------------------------------------------------

    def _kubectl_succeeds(self, *args: str) -> bool:
        try:
            completed = subprocess.run(
                [self.settings.kubectl_command, *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        return completed.returncode == 0

    def _project_environment_exists(self, namespace: str) -> bool:
        return self._kubectl_succeeds("get", "namespace", namespace) and self._kubectl_succeeds(
            "get",
            "deployment",
            OpenCodeManifestParser.CONTAINER_NAME,
            "-n",
            namespace,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init_agentis(self) -> dict[str, str | None]:
        workspace = self.workspace_path
        config_path = workspace / self.AGENTIS_CONFIG_NAME
        if config_path.exists():
            return {
                "action": "init_agentis",
                "task_id": self.context.task_id,
                "working_dir": str(workspace),
                "config_path": str(config_path),
                "status": "skipped",
                "reason": "config_exists",
            }

        template_path = self.AGENTIS_CONFIG_TEMPLATE
        if not template_path.is_file():
            raise RuntimeError(f"Agentis OpenCode template not found: {template_path}")
        if not workspace.is_dir():
            raise RuntimeError(f"working_dir does not exist: {workspace}")

        shutil.copyfile(template_path, config_path)
        return {
            "action": "init_agentis",
            "task_id": self.context.task_id,
            "working_dir": str(workspace),
            "config_path": str(config_path),
            "template_path": str(template_path),
            "status": "copied",
        }

    def deploy(self) -> dict[str, str | None]:
        log_json(
            "INFO",
            "Deploying task to Kubernetes",
            task_id=self.context.task_id,
            project_slug=self.context.project_slug,
            base_branch=self.context.base_branch,
        )
        namespace = self.namespace_for_context(self.context, self.settings)
        workspace = self.workspace_path
        workspace_path = str(workspace)
        main_dir = workspace_path if self.is_project_scope(self.context) else self.context.working_dir

        if self._should_use_local_opencode():
            runtime = self._spawn_local_opencode(workspace)
            return {
                "action": "deploy",
                "task_id": self.context.task_id,
                "base_branch": self.context.base_branch,
                "namespace": namespace,
                "manifest_path": None,
                "working_dir": workspace_path,
                "status": "local",
                "url": runtime.url,
            }

        manifest_path = str(self._resolve_manifest_source())

        if self.is_project_scope(self.context) and self._project_environment_exists(namespace):
            return {
                "action": "deploy",
                "task_id": self.context.task_id,
                "base_branch": self.context.base_branch,
                "namespace": namespace,
                "manifest_path": manifest_path,
                "working_dir": workspace_path,
                "status": "reused",
            }

        OpenCodeManifestParser(
            namespace=namespace,
            workspace_path=workspace_path,
            main_dir=main_dir,
            agentis_url=self.settings.public_base_url,
        ).apply(manifest_path)
        return {
            "action": "deploy",
            "task_id": self.context.task_id,
            "base_branch": self.context.base_branch,
            "namespace": namespace,
            "manifest_path": manifest_path,
            "working_dir": workspace_path,
        }

    def wait_ready(self, timeout: float = 300.0, interval: float = 2.0) -> dict[str, str | None]:
        url = self._opencode_url()
        log_json("INFO", "Waiting for OpenCode to become ready", url=url, timeout=timeout)

        deadline = time.monotonic() + timeout
        last_error: Exception | None = None

        while True:
            try:
                with urllib.request.urlopen(url, timeout=5.0) as response:
                    status_code = response.getcode()
                    if status_code is not None and 200 <= status_code < 500:
                        log_json("INFO", "OpenCode is ready", url=url)
                        return {
                            "action": "wait_ready",
                            "task_id": self.context.task_id,
                            "url": url,
                        }
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
                OSError,
            ) as exc:
                last_error = exc

            if time.monotonic() >= deadline:
                break

            time.sleep(interval)

        error_suffix = f" Last error: {last_error}" if last_error else ""
        raise TimeoutError(f"OpenCode is not ready within {timeout}s at {url}.{error_suffix}")

    def teardown(self) -> dict[str, Any]:
        """Tear down only the Kubernetes side (namespace/manifest/local process).

        Git worktree cleanup is the caller's responsibility (it lives in the git
        adapter layer). Returns the Kubernetes-specific fields to merge into the
        caller's ``close`` result.
        """
        namespace = self.namespace_for_context(self.context, self.settings)
        if self._should_use_local_opencode():
            stopped = self._stop_local_opencode()
            return {"namespace": namespace, "manifest_path": None, "local_process_stopped": stopped}

        manifest_path = str(self._resolve_manifest_source())
        if not self.is_project_scope(self.context):
            self.delete_manifest(manifest_path)
        return {"namespace": namespace, "manifest_path": manifest_path}

    def delete_manifest(self, manifest_path: str) -> None:
        OpenCodeManifestParser(
            namespace=self.namespace_for_context(self.context, self.settings),
            workspace_path=str(self.workspace_path),
            main_dir=self.context.working_dir,
            agentis_url=self.settings.public_base_url,
        ).delete(manifest_path, ignore_not_found=True)


__all__ = ["KubernetesRuntime", "LocalOpenCodeRuntime"]
