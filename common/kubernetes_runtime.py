"""Kubernetes/OpenCode-web adapter.

``KubernetesAdapterService`` is a regular node in the adapter inheritance tree
(``BaseAdapterService`` → ``GitAdapterService`` → here). It drives the OpenCode
*web* runtime over its REST API and delegates all Kubernetes deploy/teardown
plumbing to a composed :class:`KubernetesRuntime` helper. The local CLI adapter
composes the very same helper for its ``kubernetes`` fallback, so no adapter
borrows Kubernetes wiring from another adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.git_adapter import GitAdapterService
from common.adapter_base import log_json
from common.artifacts.source_snapshot import build_snapshot_key, snapshot_sources_best_effort
from common.opencode_rest_client import OpenCodeApiError, OpenCodeRestClient
from common.kubernetes.runtime import KubernetesRuntime, LocalOpenCodeRuntime
from opencode.utils import OpenCodeUtils


class KubernetesAdapterService(GitAdapterService):
    requires_agentis_init = True

    # ------------------------------------------------------------------
    # Kubernetes runtime collaborator
    # ------------------------------------------------------------------

    def _runtime(self, workspace_path: Path | None = None) -> KubernetesRuntime:
        workspace = workspace_path if workspace_path is not None else self._workspace_path()
        return KubernetesRuntime(self.context, self.settings, workspace)

    @classmethod
    def namespace_for_context(cls, context: AgentExecutionContextPayload, settings: Settings) -> str:
        return KubernetesRuntime.namespace_for_context(context, settings)

    @classmethod
    def opencode_url_for_context(cls, context: AgentExecutionContextPayload, settings: Settings) -> str:
        return KubernetesRuntime.opencode_url_for_context(context, settings)

    @classmethod
    def dev_server_url_for_context(cls, context: AgentExecutionContextPayload, settings: Settings) -> str:
        return KubernetesRuntime.dev_server_url_for_context(context, settings)

    def _prompt_variant(self) -> str | None:
        if not self.context.adapter or not self.context.adapter.variant:
            return None
        return self.context.adapter.variant

    # ------------------------------------------------------------------
    # Deploy / readiness — delegated to the runtime helper
    # ------------------------------------------------------------------

    def init_agentis(self) -> dict[str, str | None]:
        return self._runtime().init_agentis()

    def deploy(self) -> dict[str, str | None]:
        return self._runtime().deploy()

    def wait_ready(self, timeout: float = 300.0, interval: float = 2.0) -> dict[str, str | None]:
        return self._runtime().wait_ready(timeout=timeout, interval=interval)

    # ------------------------------------------------------------------
    # OpenCode-web session lifecycle (REST)
    # ------------------------------------------------------------------

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
        pod_url = self._runtime()._opencode_url()
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

    # ------------------------------------------------------------------
    # Tear-down — Kubernetes namespace (runtime) + git worktree (GitAdapter)
    # ------------------------------------------------------------------

    def close(self) -> dict[str, Any]:
        """Tear down the Kubernetes namespace and remove the git branch/worktree."""
        runtime = self._runtime()
        namespace = self.namespace_for_context(self.context, self.settings)
        if runtime._should_use_local_opencode():
            local_process_stopped = runtime._stop_local_opencode()
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

        manifest_path = str(runtime._resolve_manifest_source())
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

        runtime.delete_manifest(manifest_path)

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


__all__ = ["KubernetesAdapterService", "KubernetesRuntime", "LocalOpenCodeRuntime"]
