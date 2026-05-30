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
from uuid import uuid4

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.adapter_base import BaseAdapterService, log_json
from common.artifacts.source_snapshot import build_snapshot_key, snapshot_sources_best_effort
from common.agentis import AgentisJsonRpcClient
from common.opencode_rest_client import OpenCodeApiError, OpenCodeRestClient
from common.kubernetes.deploy_config import load_deploy_config
from common.kubernetes.manifest_parser import OpenCodeManifestParser
from common.local_setup import build_local_setup_shell_command
from opencode.utils import OpenCodeUtils


@dataclass
class LocalOpenCodeRuntime:
    process: subprocess.Popen[str]
    url: str
    workspace_path: str


class KubernetesAdapterService(BaseAdapterService):
    DEFAULT_MANIFEST_NAME = "opencode.yaml"
    PROJECT_MANIFEST_NAME = "opencode-project.yaml"
    AGENTIS_CONFIG_NAME = "opencode.json"
    AGENTIS_CONFIG_TEMPLATE = Path(__file__).resolve().parents[1] / "opencode.json.tpl"
    LOCAL_RUNTIME = "local"
    requires_agentis_init = True
    _local_runtimes: dict[str, LocalOpenCodeRuntime] = {}

    def _agentis_client_class(self) -> Any:
        return AgentisJsonRpcClient

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
        workspace_path = self._workspace_path()
        roots = [workspace_path]
        if not self.is_project_scope(self.context):
            source_root = Path(self.context.working_dir)
            if source_root != workspace_path:
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

    def _opencode_url(self) -> str:
        if self._is_local_runtime():
            runtime = self._local_runtimes.get(self._runtime_key(self.context))
            if runtime is not None and runtime.process.poll() is None:
                return runtime.url
        return self.opencode_url_for_context(self.context, self.settings)

    def _prompt_variant(self) -> str | None:
        if not self.context.adapter or not self.context.adapter.variant:
            return None
        return self.context.adapter.variant

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

    def init_agentis(self) -> dict[str, str | None]:
        workspace = self._workspace_path()
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
        workspace = self._workspace_path()
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

    def add_message(self, message: str, pod_url: str | None = None) -> dict[str, str | None]:
        if not pod_url:
            raise RuntimeError("pod_url is required to add messages")
        working_dir = str(self._workspace_path())
        adapter_opts = self.context.adapter

        client = OpenCodeRestClient(
            base_url=pod_url,
            directory=working_dir,
        )

        session_id = self.context.session_id
        if not session_id:
            raise RuntimeError("Context must include session_id to add messages")
        snapshot_key = build_snapshot_key(
            "opencode", self.context.run_id, self.context.task_id, session_id, uuid4().hex
        )
        snapshot_sources_best_effort(working_dir, snapshot_key, label="opencode-add-message")

        prompt_parts = [{"type": "text", "text": message}]
        prompt_body: dict[str, Any] = {"parts": prompt_parts}

        if adapter_opts and adapter_opts.model:
            model_obj = OpenCodeUtils.parse_model(adapter_opts.model)
            if model_obj:
                prompt_body["model"] = model_obj
        if adapter_opts and adapter_opts.agent:
            prompt_body["agent"] = adapter_opts.agent
        prompt_variant = self._prompt_variant()
        if prompt_variant:
            prompt_body["variant"] = prompt_variant

        try:
            log_json(
                "INFO",
                "Dispatching prompt",
                task_id=self.context.task_id,
                session_id=session_id,
                prompt=prompt_body,
            )
            client.session_prompt_async(session_id, prompt_body)
        except (OpenCodeApiError, ValueError) as exc:
            log_json("WARN", "session_prompt_async unavailable, trying sync", error=str(exc))
            try:
                client.session_prompt(session_id, prompt_body, timeout=10.0)
            except OpenCodeApiError as exc2:
                raise RuntimeError(f"Failed to send initial prompt: {exc2}") from exc2

        log_json(
            "INFO",
            "Prompt dispatched",
            task_id=self.context.task_id,
            session_id=session_id,
        )

        return {
            "action": "add_message",
            "task_id": self.context.task_id,
            "session_id": session_id,
            "pod_url": pod_url,
            "snapshot_key": snapshot_key,
        }

    def question_reply(
        self,
        request_id: str,
        answers: list[list[str]],
        pod_url: str | None = None,
    ) -> dict[str, Any]:
        if not pod_url:
            raise RuntimeError("pod_url is required to reply to questions")
        working_dir = str(self._workspace_path())
        session_id = self.context.session_id
        if not session_id:
            raise RuntimeError("Context must include session_id to reply to questions")

        client = OpenCodeRestClient(
            base_url=pod_url,
            directory=working_dir,
        )

        try:
            result = client.question_reply(request_id, answers)
        except OpenCodeApiError as exc:
            raise RuntimeError(f"Failed to reply to OpenCode question: {exc}") from exc

        log_json(
            "INFO",
            "Question reply dispatched",
            task_id=self.context.task_id,
            session_id=session_id,
            request_id=request_id,
        )

        return {
            "action": "question_reply",
            "task_id": self.context.task_id,
            "session_id": session_id,
            "request_id": request_id,
            "answers": answers,
            "pod_url": pod_url,
            "result": result,
        }

    def start_session(self, pod_url: str | None = None, fork_from_session_id: str | None = None) -> dict[str, str | None]:
        """Create an OpenCode session and send the initial prompt asynchronously."""
        if not pod_url:
            raise RuntimeError("pod_url is required to start an OpenCode session")
        working_dir = str(self._workspace_path())
        adapter_opts = self.context.adapter

        client = OpenCodeRestClient(
            base_url=pod_url,
            directory=working_dir,
        )

        source_session_id = fork_from_session_id.strip() if isinstance(fork_from_session_id, str) else ""
        try:
            if source_session_id:
                session_response = client.session_fork(source_session_id)
            else:
                session_data: dict[str, Any] = {"title": self.context.title}
                session_response = client.session_create(session_data)
        except OpenCodeApiError as exc:
            action = "fork" if source_session_id else "create"
            raise RuntimeError(f"Failed to {action} OpenCode session: {exc}") from exc

        session_id: str | None = OpenCodeUtils.extract_session_id(session_response)
        if session_id:
            self.context.session_id = session_id
        if not session_id:
            raise RuntimeError(f"OpenCode session_create returned no ID: {session_response!r}")

        log_json(
            "INFO",
            "OpenCode session created",
            task_id=self.context.task_id,
            session_id=session_id,
            fork_from_session_id=source_session_id or None,
        )
        snapshot_key = build_snapshot_key("opencode", self.context.run_id, self.context.task_id, session_id, "start")
        snapshot_sources_best_effort(working_dir, snapshot_key, label="opencode-start")
        self._persist_agentis_session_id(session_id)

        comments_block = OpenCodeUtils.build_comments_block(self.context.comments)
        prompt_parts = OpenCodeUtils.build_text_parts(
            self.context.user_prompt, self.context.description, comments_block
        )
        if not prompt_parts:
            prompt_parts = OpenCodeUtils.build_text_parts(self.context.title, comments_block) or [
                {"type": "text", "text": self.context.title}
            ]

        files_parts = OpenCodeUtils.build_attachments_parts(self.context.attachments)
        prompt_parts.extend(files_parts)

        prompt_body: dict[str, Any] = {"parts": prompt_parts}

        if adapter_opts and adapter_opts.model:
            model_obj = OpenCodeUtils.parse_model(adapter_opts.model)
            if model_obj:
                prompt_body["model"] = model_obj
        if adapter_opts and adapter_opts.agent:
            prompt_body["agent"] = adapter_opts.agent
        prompt_variant = self._prompt_variant()
        if prompt_variant:
            prompt_body["variant"] = prompt_variant

        try:
            log_json(
                "INFO",
                "Dispatching initial prompt",
                task_id=self.context.task_id,
                session_id=session_id,
                prompt=prompt_body,
            )
            client.session_prompt_async(session_id, prompt_body)
        except (OpenCodeApiError, ValueError) as exc:
            log_json("WARN", "session_prompt_async unavailable, trying sync", error=str(exc))
            try:
                client.session_prompt(session_id, prompt_body, timeout=10.0)
            except OpenCodeApiError as exc2:
                raise RuntimeError(f"Failed to send initial prompt: {exc2}") from exc2

        log_json(
            "INFO",
            "Initial prompt dispatched",
            task_id=self.context.task_id,
            session_id=session_id,
        )

        return {
            "action": "start_session",
            "task_id": self.context.task_id,
            "session_id": session_id,
            "pod_url": pod_url,
            "snapshot_key": snapshot_key,
            "fork_from_session_id": source_session_id or None,
        }

    def abort(self, session_id: str) -> dict[str, str | None]:
        working_dir = str(self._workspace_path())
        pod_url = self._opencode_url()
        client = OpenCodeRestClient(
            base_url=pod_url,
            directory=working_dir,
        )

        try:
            client.session_abort(session_id)
        except OpenCodeApiError as exc:
            raise RuntimeError(f"Failed to abort OpenCode session: {exc}") from exc

        log_json(
            "INFO",
            "OpenCode session aborted",
            task_id=self.context.task_id,
            session_id=session_id,
            pod_url=pod_url,
        )

        return {
            "action": "abort",
            "task_id": self.context.task_id,
            "session_id": session_id,
            "pod_url": pod_url,
        }

    def close(self) -> dict[str, Any]:
        """Tear down the Kubernetes namespace and remove the git branch/worktree."""
        namespace = self.namespace_for_context(self.context, self.settings)
        if self._should_use_local_opencode():
            local_process_stopped = self._stop_local_opencode()
            if self.is_project_scope(self.context):
                return {
                    "action": "close",
                    "task_id": self.context.task_id,
                    "namespace": namespace,
                    "manifest_path": None,
                    "status": "skipped",
                    "reason": "project_scope",
                    "local_process_stopped": local_process_stopped,
                    "worktree_removed": False,
                    "branch_deleted": False,
                }

            repository_root = self._repository_root()
            branch_name = self._branch_name_for_context(self.context)
            worktree_path = self._resolved_worktree_path()
            worktree_removed, branch_deleted = self._cleanup_worktree_branch(
                repository_root,
                branch_name,
                worktree_path,
            )
            return {
                "action": "close",
                "task_id": self.context.task_id,
                "branch": branch_name,
                "base_branch": self.context.base_branch,
                "namespace": namespace,
                "manifest_path": None,
                "worktree_path": str(worktree_path),
                "local_process_stopped": local_process_stopped,
                "worktree_removed": worktree_removed,
                "branch_deleted": branch_deleted,
            }

        manifest_path = str(self._resolve_manifest_source())
        if self.is_project_scope(self.context):
            return {
                "action": "close",
                "task_id": self.context.task_id,
                "namespace": namespace,
                "manifest_path": manifest_path,
                "status": "skipped",
                "reason": "project_scope",
                "worktree_removed": False,
                "branch_deleted": False,
            }

        repository_root = self._repository_root()
        branch_name = self._branch_name_for_context(self.context)
        worktree_path = self._resolved_worktree_path()

        log_json(
            "INFO",
            "Closing task environment",
            task_id=self.context.task_id,
            branch=branch_name,
            namespace=namespace,
            manifest_path=manifest_path,
            worktree_path=str(worktree_path),
        )

        OpenCodeManifestParser(
            namespace=namespace,
            workspace_path=str(worktree_path),
            main_dir=self.context.working_dir,
            agentis_url=self.settings.public_base_url,
        ).delete(manifest_path, ignore_not_found=True)

        worktree_removed, branch_deleted = self._cleanup_worktree_branch(
            repository_root,
            branch_name,
            worktree_path,
        )

        return {
            "action": "close",
            "task_id": self.context.task_id,
            "branch": branch_name,
            "base_branch": self.context.base_branch,
            "namespace": namespace,
            "manifest_path": manifest_path,
            "worktree_path": str(worktree_path),
            "worktree_removed": worktree_removed,
            "branch_deleted": branch_deleted,
        }
