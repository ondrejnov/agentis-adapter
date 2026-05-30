"""InternalRpcService – handles JSON-RPC calls from the opencode plugin (/api-internal).

Each method is a stub; implementation will be added in a later phase.
All calls are logged to /tmp/agentis-internal-rpc.log for structure documentation.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from common.models import (
    AddQuestionParams,
    AgentExecutionContextPayload,
    SessionCreatedParams,
    SessionErrorParams,
    SessionIdleParams,
    SessionUpdateParams,
    StartTaskParams,
    StoreActivityLogParams,
    completion_task_status,
)
from common.config import Settings
from common.kubernetes_runtime import KubernetesAdapterService
from common.artifacts.screenshots import collect_screenshot_images
from common.artifacts.expected import collect_expected_artifacts
from common.artifacts.source_snapshot import changes_diff_attachment, write_changes_diff_best_effort
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.integrations.github_pr import GithubPrError, GithubPrResult, GithubPrService
from common.kubernetes.manifest_parser import OpenCodeManifestParser
from opencode.utils import OpenCodeUtils
from common.rpc.jsonrpc import AgentJsonRpcException
from common.rpc.session_registry import SessionContextRegistry

_LOG_FILE = Path("/tmp/agentis-internal-rpc.log")
_ALLOWED_ADAPTER_EVENT_STATUSES = {"started", "success", "failed"}


def _log_call(method: str, params: object) -> None:
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "method": method,
            "params": json.loads(params.model_dump_json()),  # type: ignore[attr-defined]
        }
        line = json.dumps(entry, ensure_ascii=False)
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[internal] log_call failed: {exc}\n")


class InternalRpcService:
    def __init__(
        self,
        settings: Settings,
        timeout: float = 10.0,
        session_registry: SessionContextRegistry | None = None,
    ):
        self.settings = settings
        self.timeout = timeout
        self.session_registry = session_registry or SessionContextRegistry()

    @staticmethod
    def _normalize_string(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AgentJsonRpcException(400, f"{field_name} is required")
        return value.strip()

    @staticmethod
    def _normalize_adapter_event_status(status: str) -> str:
        normalized = status.strip().lower()
        if normalized == "skipped":
            return "success"
        if normalized in _ALLOWED_ADAPTER_EVENT_STATUSES:
            return normalized
        raise AgentJsonRpcException(500, f"Unsupported adapter event status: {status}")

    @staticmethod
    def _extract_json_from_text(text: str) -> Any | None:
        decoder = json.JSONDecoder()
        stripped = text.strip()
        if not stripped:
            return None

        try:
            value, end = decoder.raw_decode(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if stripped[end:].strip() == "":
                return value

        for index, char in enumerate(stripped):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            return value

        return None

    def _collect_screenshot_images(self, context: AgentExecutionContextPayload | None) -> list[dict[str, Any]]:
        if context is None:
            return []
        try:
            project_root = self._worktree_path(context)
        except Exception:  # noqa: BLE001 - screenshots are best-effort and must not break completion comments
            project_root = context.working_dir
        return collect_screenshot_images(project_root)

    def _collect_expected_artifacts(self, context: AgentExecutionContextPayload | None) -> list[dict[str, Any]]:
        if context is None:
            return []
        try:
            project_root = self._worktree_path(context)
        except Exception:  # noqa: BLE001 - artifacts are best-effort and must not break completion comments
            project_root = context.working_dir
        return collect_expected_artifacts(context, project_root)

    def _forward(self, request_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.agentis_endpoint:
            raise AgentJsonRpcException(400, "agentis_endpoint is missing in adapter settings")

        endpoint = self.settings.agentis_endpoint
        try:
            with AgentisJsonRpcClient(
                endpoint=endpoint,
                token=self.settings.agentis_token,
                timeout=self.timeout,
            ) as client:
                result = client.call(method=method, params=params, request_id=request_id)
        except AgentisJsonRpcError as exc:
            raise AgentJsonRpcException(
                502,
                f"Failed to forward `{method}` to Agentis: {exc}",
                exc.details,
            ) from exc

        return result if isinstance(result, dict) else {"ok": True, "result": result}

    def _update_task_knowledge(self, context: AgentExecutionContextPayload, knowledge: Any | None) -> None:
        if knowledge is not None and not isinstance(knowledge, dict):
            sys.stderr.write(
                "[internal] knowledge extractor returned non-object JSON; skipping task knowledge update\n"
            )
            return

        self._forward(
            request_id=f"internal-update-related-knowledge-{context.task_id}",
            method="task.update_related_knowledge",
            params={"id": context.task_id, "knowledge": knowledge},
        )

    def _ensure_pull_request(self, context: AgentExecutionContextPayload) -> GithubPrResult | None:
        """Create or look up a GitHub PR for the task branch. Returns metadata or None on failure."""
        if KubernetesAdapterService.is_project_scope(context):
            return None
        if not context.project_github_repo:
            return None

        try:
            worktree_path = self._worktree_path(context)
            branch = KubernetesAdapterService._branch_name_for_context(context)
            service = GithubPrService(context=context, worktree_path=worktree_path, branch=branch)
            return service.ensure_pull_request_result()
        except GithubPrError as exc:
            sys.stderr.write(f"[internal] ensure_pull_request failed: {exc}\n")
            return None
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[internal] ensure_pull_request unexpected error: {exc}\n")
            return None

    def _worktree_path(self, context: AgentExecutionContextPayload) -> Path:
        if KubernetesAdapterService.is_project_scope(context):
            try:
                return Path(
                    KubernetesAdapterService._run_git(
                        Path(context.working_dir),
                        "rev-parse",
                        "--show-toplevel",
                    )
                )
            except RuntimeError:
                return Path(context.working_dir)
        return self.settings.worktree_root / KubernetesAdapterService._task_safe_name(context.task_id)

    @staticmethod
    def _completion_actions(context: AgentExecutionContextPayload | None = None) -> list[dict[str, Any]]:
        if context is not None and KubernetesAdapterService.is_project_scope(context):
            return []
        return [
            {
                "title": "Git merge",
                "prompt": "Sloučit změny z task větve do hlavní větve.",
                "adapter_method": "git_merge",
                "continue_previous_run": False,
            },
            {
                "title": "Zavřít prostředí",
                "prompt": "Uklidit Kubernetes namespace, worktree a task větev.",
                "adapter_method": "close",
                "continue_previous_run": False,
            },
        ]

    def _run_kubectl(self, *args: str) -> str:
        completed = subprocess.run(
            ["kubectl", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown kubectl error"
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {stderr}")
        return completed.stdout.strip()

    def _resolve_opencode_pod_name(self, namespace: str) -> str:
        pod_name = self._run_kubectl(
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app=opencode",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        )
        if not pod_name:
            raise RuntimeError(f"No opencode pod found in namespace {namespace}")
        return pod_name

    def _start_dev_server(self, context: AgentExecutionContextPayload) -> dict[str, Any]:
        if shutil.which("kubectl") is None:
            raise RuntimeError("kubectl CLI is not available on PATH")

        namespace = KubernetesAdapterService.namespace_for_context(context, self.settings)
        if not namespace:
            raise RuntimeError("namespace is required to start the dev server")

        worktree_path = self._worktree_path(context)
        pod_name = self._resolve_opencode_pod_name(namespace)
        output = self._run_kubectl(
            "exec",
            "-n",
            namespace,
            pod_name,
            "-c",
            OpenCodeManifestParser.CONTAINER_NAME,
            "--",
            "sh",
            "-lc",
            f"cd {shlex.quote(str(worktree_path))} && ./run-dev.sh",
        )

        result: dict[str, Any] = {
            "namespace": namespace,
            "pod_name": pod_name,
            "working_dir": str(worktree_path),
        }
        if output:
            result["output"] = output
        return result

    def _commit_session_changes(self, context: AgentExecutionContextPayload) -> dict[str, Any]:
        worktree_path = self._worktree_path(context)
        if not worktree_path.is_dir():
            return {
                "status": "skipped",
                "reason": "missing_worktree",
                "working_dir": str(worktree_path),
            }

        if not KubernetesAdapterService._git_succeeds(worktree_path, "rev-parse", "--is-inside-work-tree"):
            return {
                "status": "skipped",
                "reason": "not_a_git_worktree",
                "working_dir": str(worktree_path),
            }

        if not KubernetesAdapterService._run_git(worktree_path, "status", "--porcelain"):
            return {
                "status": "skipped",
                "reason": "clean_worktree",
                "working_dir": str(worktree_path),
            }

        commit_message = f"TASK: #{context.task_number} - {context.title}"
        KubernetesAdapterService._run_git(worktree_path, "add", "--all")
        KubernetesAdapterService._run_git(
            worktree_path,
            "-c",
            "user.name=Agentis",
            "-c",
            "user.email=code@agentis.cz",
            "commit",
            "-m",
            commit_message,
        )
        commit_sha = KubernetesAdapterService._run_git(worktree_path, "rev-parse", "HEAD")
        return {
            "status": "success",
            "working_dir": str(worktree_path),
            "commit_sha": commit_sha,
            "commit_message": commit_message,
        }

    def _emit_adapter_event(
        self,
        context: AgentExecutionContextPayload | None,
        *,
        kind: str,
        status: str,
        event_id: str | None = None,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if context is None:
            return

        normalized_event_id = event_id or f"{kind}:{uuid4().hex}"

        self._forward(
            request_id=f"internal-adapter-event-{context.run_id}-{normalized_event_id}-{status}",
            method="run.adapter_event",
            params={
                "run_id": context.run_id,
                "kind": kind,
                "status": self._normalize_adapter_event_status(status),
                "event_id": normalized_event_id,
                "message": message,
                "data": data or {},
            },
        )

    # ------------------------------------------------------------------
    # coding_session.*
    # ------------------------------------------------------------------

    def start_task(self, params: StartTaskParams) -> dict:
        """Mark a task as started and associate it with an opencode session."""
        _log_call("session.start_task", params)
        self._normalize_string(params.id, "id")
        self._normalize_string(params.session_id, "session_id")
        return {"ok": True, "method": "session.start_task", "forwarded": False}

    def session_idle(self, params: SessionIdleParams) -> dict:
        """Handle session.idle event from the plugin."""
        _log_call("session.session_idle", params)
        session_id = self._normalize_string(params.session_id, "session_id")
        context = self.session_registry.get(session_id)
        self._forward(
            request_id=f"internal-store-activity-log-{session_id}",
            method="session.store_activity_log",
            params={"session_id": session_id, "messages": params.messages},
        )

        last_message = params.messages[-1] if params.messages else None
        idle_event_id = f"idle:{session_id}:{uuid4().hex}"

        start_dev_server = context is not None and not KubernetesAdapterService.is_project_scope(context)
        if context is not None and context.adapter and context.adapter.agent == "knowledge-extractor":
            start_dev_server = False

        if last_message is not None:
            tags = []
            commit_event_id = f"commit:{session_id}:{uuid4().hex}"
            dev_server_event_id = f"dev_server:{session_id}:{uuid4().hex}"

            if context is not None and context.project_github_repo:
                if not KubernetesAdapterService.is_project_scope(context):
                    if context.ide:
                        ide = context.ide.strip().replace("[%WORKDIR%]", str(self._worktree_path(context)))
                        tags.append({"label": "Directory", "value": ide, "type": "url"})

                    try:
                        commit_result = self._commit_session_changes(context)
                    except Exception as exc:  # noqa: BLE001
                        self._emit_adapter_event(
                            context,
                            kind="commit",
                            status="failed",
                            event_id=commit_event_id,
                            message="Commit rozpracovaného kódu selhal.",
                            data={"session_id": session_id, "error": str(exc)},
                        )
                    else:
                        commit_status = str(commit_result.get("status") or "skipped")
                        reason = str(commit_result.get("reason") or "")
                        commit_message = "Rozpracovaný kód byl commitnut."
                        if commit_status == "skipped":
                            if reason == "missing_worktree":
                                commit_message = "Worktree pro session není k dispozici, commit přeskočen."
                            elif reason == "not_a_git_worktree":
                                commit_message = "Session worktree není git repozitář, commit přeskočen."
                            else:
                                commit_message = "Žádné změny ke commitnutí."

                        self._emit_adapter_event(
                            context,
                            kind="commit",
                            status=commit_status,
                            event_id=commit_event_id,
                            message=commit_message,
                            data={"session_id": session_id, **commit_result},
                        )

                    pr_result = self._ensure_pull_request(context)
                    if pr_result:
                        tags.append(
                            {
                                "label": "Pull Request",
                                "value": pr_result.url + "/changes",
                                "type": "url",
                            }
                        )

                if start_dev_server:
                    self._emit_adapter_event(
                        context,
                        kind="dev_server",
                        status="started",
                        event_id=dev_server_event_id,
                        message="Spouštím dev server.",
                    )
                    try:
                        dev_server_result = self._start_dev_server(context)
                    except Exception as exc:  # noqa: BLE001
                        self._emit_adapter_event(
                            context,
                            kind="dev_server",
                            status="failed",
                            event_id=dev_server_event_id,
                            message="Spuštění dev serveru selhalo.",
                            data={"error": str(exc)},
                        )
                    else:
                        self._emit_adapter_event(
                            context,
                            kind="dev_server",
                            status="success",
                            event_id=dev_server_event_id,
                            message="Dev server byl spuštěn.",
                            data=dev_server_result if isinstance(dev_server_result, dict) else None,
                        )
                        tags.append(
                            {
                                "label": "Dev server",
                                "type": "url",
                                "value": KubernetesAdapterService.dev_server_url_for_context(context, self.settings),
                            }
                        )

            snapshot_key = self.session_registry.get_snapshot_key(session_id)
            if context is not None and snapshot_key:
                try:
                    diff_result = write_changes_diff_best_effort(
                        self._worktree_path(context),
                        snapshot_key,
                        label="opencode-idle",
                    )
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(f"[internal] write changes diff failed: {exc}\n")
                else:
                    diff_attachment = changes_diff_attachment(diff_result)
                    if diff_attachment:
                        tags.append(diff_attachment)

            body = OpenCodeUtils.extract_message_text(last_message)
            self._forward(
                request_id=f"internal-session-idle-{session_id}",
                method="task.add_agent_comment",
                params={
                    "session_id": session_id,
                    "body": body,
                    "attachments": tags,
                    "images": self._collect_screenshot_images(context),
                    "artifacts": self._collect_expected_artifacts(context),
                    "comment_type": "primary",
                    "actions": self._completion_actions(context),
                    "status": completion_task_status(context),
                },
            )

            if context is not None and context.adapter and context.adapter.agent == "knowledge-extractor":
                knowledge_json = self._extract_json_from_text(body)
                self._update_task_knowledge(context, knowledge_json)
                self._emit_adapter_event(
                    context,
                    kind="knowledge-extractor",
                    status="success",
                    message="Knowledge extracted",
                    data={"session_id": session_id, "knowledge": knowledge_json},
                )

        self._emit_adapter_event(
            context,
            kind="idle",
            status="success",
            event_id=idle_event_id,
            message="OpenCode session je neaktivní.",
            data={"session_id": session_id, "message_count": len(params.messages)},
        )

        return {"ok": True}

    def session_update(self, params: SessionUpdateParams) -> dict:
        """Handle session.updated event from the plugin."""
        _log_call("session.session_update", params)
        session_id = self._normalize_string(params.session_id, "session_id")
        return self._forward(
            request_id=f"internal-session-update-{session_id}",
            method="session.session_update",
            params={"session_id": session_id, "session": params.session},
        )

    def session_error(self, params: SessionErrorParams) -> dict:
        """Handle session.error event synthesized by the plugin."""
        _log_call("session.session_error", params)
        properties = params.properties if isinstance(params.properties, dict) else {}
        session_id = self._normalize_string(
            properties.get("sessionID") or properties.get("session_id"),
            "properties.sessionID",
        )
        return self._forward(
            request_id=f"internal-session-error-{session_id}",
            method="session.session_error",
            params={
                "type": params.type,
                "properties": {**properties, "sessionID": session_id},
            },
        )

    def session_created(self, params: SessionCreatedParams) -> dict:
        """Handle session.created event from the plugin."""
        _log_call("session.session_created", params)
        session = params.session if isinstance(params.session, dict) else {}
        session_id = self._normalize_string(session.get("id"), "session.id")
        return self._forward(
            request_id=f"internal-session-created-{session_id}",
            method="session.session_created",
            params={"session": session},
        )

    def store_activity_log(self, params: StoreActivityLogParams) -> dict:
        """Persist activity messages for a session."""
        _log_call("session.store_activity_log", params)
        session_id = self._normalize_string(params.session_id, "session_id")
        return self._forward(
            request_id=f"internal-store-activity-log-{session_id}",
            method="session.store_activity_log",
            params={"session_id": session_id, "messages": params.messages},
        )

    def add_question(self, params: AddQuestionParams) -> dict:
        """Forward pending OpenCode question metadata to Agentis."""
        _log_call("task.add_question", params)
        external_id = self._normalize_string(params.external_id, "external_id")
        session_id = self._normalize_string(params.session_id, "session_id")
        self._normalize_string(params.tool.messageID, "tool.messageID")
        self._normalize_string(params.tool.callID, "tool.callID")
        return self._forward(
            request_id=f"internal-add-question-{session_id}-{external_id}",
            method="task.add_question",
            params={
                "external_id": external_id,
                "session_id": session_id,
                "questions": params.questions,
                "tool": params.tool.model_dump(),
            },
        )
