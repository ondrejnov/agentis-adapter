"""Kubernetes deploy machinery used by the adapters.

``KubernetesRuntime`` is a plain collaborator (NOT an adapter): it owns the
Kubernetes-specific concerns — namespace/ingress naming, manifest resolution and
``apply``/``delete`` and the readiness probe. Both the local CLI adapter (in
``kubernetes`` mode) and the ``KubernetesAdapterService`` compose it; neither
borrows it from the other, so the adapter inheritance tree stays free of
Kubernetes wiring.

The git/worktree layer resolves the workspace path and passes it in; this helper
never runs git itself.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.adapter_base import log_json
from common.kubernetes.ci_workflow import (
    CI_WORKFLOW_PATH,
    CiStep,
    CiWorkflow,
    build_step_job_manifest,
    load_ci_workflow,
    load_finish_workflow,
    namespace_manifest,
    step_job_name,
)
from common.kubernetes.deploy_config import load_deploy_config
from common.kubernetes.manifest_parser import OpenCodeManifestParser


class KubernetesRuntime:
    """Deploy/teardown collaborator for the Kubernetes-backed OpenCode runtime."""

    DEFAULT_MANIFEST_NAME = "opencode.yaml"
    PROJECT_MANIFEST_NAME = "opencode-project.yaml"
    AGENTIS_CONFIG_NAME = "opencode.json"
    AGENTIS_CONFIG_TEMPLATE = Path(__file__).resolve().parents[2] / "opencode.json.tpl"

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

    def _resolve_manifest_source(self) -> Path:
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
    # OpenCode URL
    # ------------------------------------------------------------------

    def _opencode_url(self) -> str:
        return self.opencode_url_for_context(self.context, self.settings)

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

    def _kubectl(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.settings.kubectl_command, *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def _kubectl_apply(self, manifest: dict[str, Any]) -> None:
        document = yaml.safe_dump(manifest, sort_keys=False)
        result = self._kubectl("apply", "-f", "-", stdin=document)
        if result.returncode != 0:
            raise RuntimeError(f"kubectl apply failed: {result.stderr.strip()}")

    # ------------------------------------------------------------------
    # CI setup workflow (.agentis/ci.yaml)
    # ------------------------------------------------------------------

    def _ci_workflow_path(self) -> Path:
        return self.workspace_path / CI_WORKFLOW_PATH

    def load_ci_workflow(self) -> CiWorkflow | None:
        return load_ci_workflow(self._ci_workflow_path())

    def ci_setup_steps(self) -> list[CiStep]:
        if self.is_project_scope(self.context):
            return []
        workflow = self.load_ci_workflow()
        return list(workflow.steps) if workflow else []

    def _ensure_namespace(self, namespace: str) -> None:
        self._kubectl_apply(namespace_manifest(namespace))

    def ensure_namespace(self, namespace: str) -> None:
        """Public wrapper used by CLI adapters before applying the agent Job."""
        self._ensure_namespace(namespace)

    def run_ci_step(self, step: CiStep, timeout: float = 900.0, interval: float = 3.0) -> dict[str, Any]:
        workflow = self.load_ci_workflow()
        if workflow is None:
            raise RuntimeError(f"CI workflow not found at {self._ci_workflow_path()}")

        namespace = self.namespace_for_context(self.context, self.settings)
        self._ensure_namespace(namespace)

        job_name = step_job_name(step)
        manifest = build_step_job_manifest(
            workflow=workflow,
            step=step,
            namespace=namespace,
            workspace_path=str(self.workspace_path),
            main_dir=self.context.working_dir,
            agentis_url=self.settings.public_base_url,
        )

        log_json(
            "INFO",
            "Running CI setup step",
            task_id=self.context.task_id,
            namespace=namespace,
            step=step.id,
            step_name=step.name,
            job=job_name,
        )

        # Drop any stale job from a previous run so the manifest applies cleanly.
        self._kubectl("delete", "job", job_name, "-n", namespace, "--ignore-not-found=true", "--wait=true")
        self._kubectl_apply(manifest)
        logs = self._wait_for_job(namespace, job_name, timeout=timeout, interval=interval)

        log_json(
            "INFO",
            "CI setup step finished",
            task_id=self.context.task_id,
            namespace=namespace,
            step=step.id,
            job=job_name,
        )

        return {
            "action": "ci_setup",
            "task_id": self.context.task_id,
            "namespace": namespace,
            "step": step.id,
            "name": step.name,
            "job": job_name,
            "logs": logs,
        }

    # ------------------------------------------------------------------
    # Finish workflow (.agentis/ci.yaml ``finish:`` — commit / pull request)
    # ------------------------------------------------------------------

    def load_finish_workflow(self) -> CiWorkflow | None:
        return load_finish_workflow(self._ci_workflow_path())

    def finish_steps(self) -> list[CiStep]:
        if self.is_project_scope(self.context):
            return []
        workflow = self.load_finish_workflow()
        return list(workflow.steps) if workflow else []

    def run_finish_step(
        self,
        step: CiStep,
        *,
        extra_replacements: dict[str, str] | None = None,
        timeout: float = 600.0,
        interval: float = 3.0,
    ) -> dict[str, Any]:
        workflow = self.load_finish_workflow()
        if workflow is None:
            raise RuntimeError(f"Finish workflow not found at {self._ci_workflow_path()}")

        namespace = self.namespace_for_context(self.context, self.settings)
        self._ensure_namespace(namespace)

        job_name = step_job_name(step, prefix="finish")
        manifest = build_step_job_manifest(
            workflow=workflow,
            step=step,
            namespace=namespace,
            workspace_path=str(self.workspace_path),
            main_dir=self.context.working_dir,
            agentis_url=self.settings.public_base_url,
            extra_replacements=extra_replacements,
            job_prefix="finish",
            app_label="finish",
        )

        log_json(
            "INFO",
            "Running finish step",
            task_id=self.context.task_id,
            namespace=namespace,
            step=step.id,
            step_name=step.name,
            job=job_name,
        )

        self._kubectl("delete", "job", job_name, "-n", namespace, "--ignore-not-found=true", "--wait=true")
        self._kubectl_apply(manifest)
        logs = self._wait_for_job(namespace, job_name, timeout=timeout, interval=interval)
        attachments = self._finish_step_attachments(step)

        return {
            "action": "finish",
            "task_id": self.context.task_id,
            "namespace": namespace,
            "step": step.id,
            "name": step.name,
            "job": job_name,
            "logs": logs,
            "attachments": attachments,
        }

    def _finish_step_attachments(self, step: CiStep) -> list[dict[str, str]]:
        workspace_root = self.workspace_path.resolve()
        attachments: list[dict[str, str]] = []

        for attachment in step.attachments:
            path = (self.workspace_path / attachment.value_from).resolve()
            if not path.is_relative_to(workspace_root) or not path.is_file():
                continue
            value = path.read_text(encoding="utf-8").strip()
            if not value:
                continue
            attachments.append({"label": attachment.label, "value": value, "type": attachment.type})

        return attachments

    def _job_logs(self, namespace: str, job_name: str) -> str:
        result = self._kubectl("logs", f"job/{job_name}", "-n", namespace, "--tail=200")
        return result.stdout if result.returncode == 0 else result.stderr

    def _wait_for_job(self, namespace: str, job_name: str, *, timeout: float, interval: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            succeeded = self._kubectl(
                "get", "job", job_name, "-n", namespace, "-o", "jsonpath={.status.succeeded}"
            ).stdout.strip()
            if succeeded and succeeded != "0":
                return self._job_logs(namespace, job_name)

            failed = self._kubectl(
                "get", "job", job_name, "-n", namespace, "-o", "jsonpath={.status.failed}"
            ).stdout.strip()
            if failed and failed != "0":
                logs = self._job_logs(namespace, job_name)
                raise RuntimeError(f"CI step job {job_name} failed.\n{logs}")

            if time.monotonic() >= deadline:
                logs = self._job_logs(namespace, job_name)
                raise TimeoutError(f"CI step job {job_name} did not complete within {timeout}s.\n{logs}")

            time.sleep(interval)

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
        workspace_path = str(self.workspace_path)
        main_dir = workspace_path if self.is_project_scope(self.context) else self.context.working_dir

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
        """Tear down only the Kubernetes side (namespace/manifest).

        Git worktree cleanup is the caller's responsibility (it lives in the git
        adapter layer). Returns the Kubernetes-specific fields to merge into the
        caller's ``close`` result.
        """
        namespace = self.namespace_for_context(self.context, self.settings)
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


__all__ = ["KubernetesRuntime"]
