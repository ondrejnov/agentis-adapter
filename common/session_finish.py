"""Shared completion actions for CLI agent session managers.

Po doběhnutí jednoho agentího běhu je potřeba udělat vždy tutéž sadu věcí:
commitnout rozpracovaný worktree, založit/dohledat pull request, vytvořit diff
změn, spustit dev server a poslat finální komentář agenta se všemi těmito
přílohami do Agentisu.

Tahle logika byla původně jen v :class:`~common.session_manager.BaseSessionManager`
(Claude Code / OpenCode). Aby ji mohl použít i ``agentiscode`` adaptér — který
agenta spouští jako subproces a ne přes interní streaming klienta — je vytažená
sem do mixinu :class:`SessionFinishActions`. Oba světy tak posílají **stejné**
dokončovací akce a finální komentář.

Mixin je úmyslně agnostický vůči konkrétnímu session objektu — pracuje s čímkoli,
co splňuje :class:`SessionLike` (stejné názvy atributů sdílí ``_AgentSession`` i
``_AgentisCodeSession``). Vyžaduje od hostitelské třídy jen ``self.settings`` a
class atribut ``_AGENT_LABEL``.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Protocol
from uuid import uuid4

from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.artifacts.expected import collect_expected_artifacts
from common.artifacts.screenshots import collect_screenshot_images
from common.artifacts.source_snapshot import changes_diff_attachment, write_changes_diff_best_effort
from common.config import Settings
from common.git_adapter import GitAdapterService
from common.integrations.github_pr import GithubPrError, GithubPrResult, GithubPrService
from common.kubernetes_runtime import KubernetesAdapterService
from common.models import AgentExecutionContextPayload, completion_task_status

try:  # KubectlExecTarget je jen pro typování dev serveru.
    from common.cli_session import KubectlExecTarget
except Exception:  # pragma: no cover - typovací fallback
    KubectlExecTarget = Any  # type: ignore[assignment,misc]


_ALLOWED_ADAPTER_EVENT_STATUSES = {"started", "success", "failed"}


class SessionLike(Protocol):
    """Minimální tvar session objektu, který :class:`SessionFinishActions` potřebuje."""

    context: AgentExecutionContextPayload
    worktree: str
    session_id: Optional[str]
    snapshot_key: Optional[str]
    kubectl_target: Optional[Any]

    def _abort_is_set(self) -> bool: ...  # pragma: no cover - jen pro dokumentaci


class SessionFinishActions:
    """Mixin se sdílenými dokončovacími akcemi (commit / PR / diff / dev server / komentář).

    Hostitelská třída musí poskytnout ``self.settings`` a může přepsat
    ``_AGENT_LABEL`` (používá se v logu, snapshot klíčích a názvech adapter eventů).
    """

    settings: Settings
    _AGENT_LABEL: str = "agent"

    # ------------------------------------------------------------------
    # Finální komentář
    # ------------------------------------------------------------------

    def _finalize_and_comment(self, sess: Any, session_ref: str, *, body: str) -> None:
        """Poskládá přílohy (dokončovací akce + diff) a pošle finální komentář.

        Dokončovací akce se přeskočí, když byl běh přerušen (``abort``); finální
        komentář se i tak pošle, pokud existuje text odpovědi a session id —
        stejně jako u streamovacích adaptérů.
        """
        attachments: list[dict[str, Any]] = []
        if not sess.abort_event.is_set():
            attachments = self._finish_session_actions(sess, session_ref)
            if sess.snapshot_key:
                diff_result = write_changes_diff_best_effort(
                    sess.worktree,
                    sess.snapshot_key,
                    label=f"{self._AGENT_LABEL}-finish",
                )
                diff_attachment = changes_diff_attachment(diff_result)
                if diff_attachment:
                    attachments.append(diff_attachment)

        if body and sess.session_id:
            self._agentis_call(
                method="task.add_agent_comment",
                params={
                    "session_id": sess.session_id,
                    "body": body,
                    "attachments": attachments,
                    "images": collect_screenshot_images(sess.worktree),
                    "artifacts": collect_expected_artifacts(sess.context, sess.worktree),
                    "status": completion_task_status(sess.context),
                    "comment_type": "primary",
                    "actions": self._completion_actions(sess.context),
                },
            )

    @staticmethod
    def _extract_final_text(messages: list[dict[str, Any]]) -> str:
        if not messages:
            return ""
        for entry in reversed(messages):
            info = entry.get("info") or {}
            if info.get("role") != "assistant":
                continue
            last_text = ""
            for part in entry.get("parts") or []:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = (part.get("text") or "").strip()
                if text:
                    last_text = text
            if last_text:
                return last_text
        return ""

    @staticmethod
    def _completion_actions(context: AgentExecutionContextPayload | None = None) -> list[dict[str, Any]]:
        if context is not None and GitAdapterService.is_project_scope(context):
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

    @staticmethod
    def _normalize_adapter_event_status(status: str) -> str:
        normalized = status.strip().lower()
        if normalized == "skipped":
            return "success"
        if normalized in _ALLOWED_ADAPTER_EVENT_STATUSES:
            return normalized
        return "failed"

    def _commit_session_changes(self, context: AgentExecutionContextPayload, worktree_path: Path) -> dict[str, Any]:
        if not worktree_path.is_dir():
            return {
                "status": "skipped",
                "reason": "missing_worktree",
                "working_dir": str(worktree_path),
            }

        if not GitAdapterService._git_succeeds(worktree_path, "rev-parse", "--is-inside-work-tree"):
            return {
                "status": "skipped",
                "reason": "not_a_git_worktree",
                "working_dir": str(worktree_path),
            }

        if not GitAdapterService._run_git(worktree_path, "status", "--porcelain"):
            return {
                "status": "skipped",
                "reason": "clean_worktree",
                "working_dir": str(worktree_path),
            }

        commit_message = f"TASK: #{context.task_number} - {context.title}"
        GitAdapterService._run_git(worktree_path, "add", "--all")
        GitAdapterService._run_git(
            worktree_path,
            "-c",
            "user.name=Agentis",
            "-c",
            "user.email=code@agentis.cz",
            "commit",
            "-m",
            commit_message,
        )
        commit_sha = GitAdapterService._run_git(worktree_path, "rev-parse", "HEAD")
        return {
            "status": "success",
            "working_dir": str(worktree_path),
            "commit_sha": commit_sha,
            "commit_message": commit_message,
        }

    def _ensure_pull_request(
        self,
        context: AgentExecutionContextPayload,
        worktree_path: Path,
    ) -> GithubPrResult | None:
        if GitAdapterService.is_project_scope(context):
            return None
        if not context.project_github_repo:
            return None

        try:
            branch = GitAdapterService._branch_name_for_context(context)
            service = GithubPrService(context=context, worktree_path=worktree_path, branch=branch)
            return service.ensure_pull_request_result()
        except GithubPrError as exc:
            sys.stderr.write(f"[{self._AGENT_LABEL}-session] ensure_pull_request failed: {exc}\n")
            return None
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[{self._AGENT_LABEL}-session] ensure_pull_request unexpected error: {exc}\n")
            return None

    def _run_completed_process(self, args: list[str], *, cwd: Path | None = None) -> str:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown command error"
            raise RuntimeError(f"{' '.join(args)} failed: {stderr}")
        return completed.stdout.strip()

    def _start_dev_server(self, sess: Any) -> dict[str, Any]:
        worktree_path = Path(sess.worktree)
        if sess.kubectl_target is None:
            script = worktree_path / "run-dev.sh"
            if not script.is_file():
                raise RuntimeError(f"Dev server script {script} does not exist")
            output = self._run_completed_process(["./run-dev.sh"], cwd=worktree_path)
            result: dict[str, Any] = {"working_dir": str(worktree_path)}
            if output:
                result["output"] = output
            return result

        target = sess.kubectl_target
        if shutil.which(target.kubectl) is None and not Path(target.kubectl).is_absolute():
            raise RuntimeError(f"kubectl CLI is not available on PATH: {target.kubectl}")

        args = [target.kubectl, "-n", target.namespace, "exec", target.selector]
        if target.container:
            args.extend(["-c", target.container])
        args.extend(["--", "sh", "-lc", f"cd {shlex.quote(str(worktree_path))} && ./run-dev.sh"])
        output = self._run_completed_process(args)
        result = {
            "namespace": target.namespace,
            "selector": target.selector,
            "working_dir": str(worktree_path),
        }
        if target.container:
            result["container"] = target.container
        if output:
            result["output"] = output
        return result

    def _finish_session_actions(self, sess: Any, session_ref: str) -> list[dict[str, Any]]:
        context = sess.context
        if GitAdapterService.is_project_scope(context) or not context.project_github_repo:
            return []

        attachments: list[dict[str, Any]] = []
        worktree_path = Path(sess.worktree)

        if context.ide:
            ide = context.ide.strip().replace("[%WORKDIR%]", str(worktree_path))
            attachments.append({"label": "Directory", "value": ide, "type": "url"})

        commit_event_id = f"commit:{session_ref}:{uuid4().hex}"
        dev_server_event_id = f"dev_server:{session_ref}:{uuid4().hex}"

        try:
            commit_result = self._commit_session_changes(context, worktree_path)
        except Exception as exc:  # noqa: BLE001
            self._emit_adapter_event(
                context,
                kind="commit",
                status="failed",
                event_id=commit_event_id,
                message="Commit rozpracovaného kódu selhal.",
                data={"session_id": sess.session_id, "error": str(exc)},
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
                data={"session_id": sess.session_id, **commit_result},
            )

        pr_result = self._ensure_pull_request(context, worktree_path)
        if pr_result:
            attachments.append(
                {
                    "label": "Pull Request",
                    "value": pr_result.url + "/changes",
                    "type": "url",
                }
            )

        self._emit_adapter_event(
            context,
            kind="dev_server",
            status="started",
            event_id=dev_server_event_id,
            message="Spouštím dev server.",
        )
        try:
            dev_server_result = self._start_dev_server(sess)
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
                data=dev_server_result,
            )
            attachments.append(
                {
                    "label": "Dev server",
                    "type": "url",
                    "value": KubernetesAdapterService.dev_server_url_for_context(context, self.settings),
                }
            )

        return attachments

    # ------------------------------------------------------------------
    # Agentis RPC
    # ------------------------------------------------------------------

    def _agentis_call(self, method: str, params: dict[str, Any]) -> None:
        endpoint = self.settings.agentis_endpoint
        if not endpoint:
            return
        try:
            with AgentisJsonRpcClient(endpoint=endpoint, token=self.settings.agentis_token) as client:
                client.call(method=method, params=params, request_id=f"{self._AGENT_LABEL}-{method}-{uuid4().hex}")
        except AgentisJsonRpcError as exc:
            sys.stderr.write(f"[{self._AGENT_LABEL}-session] agentis {method} failed: {exc}\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[{self._AGENT_LABEL}-session] agentis {method} unexpected error: {exc!r}\n")

    def _emit_adapter_event(
        self,
        context: AgentExecutionContextPayload | None,
        *,
        kind: str,
        status: str,
        event_id: str,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if context is None or not context.run_id:
            return
        self._agentis_call(
            method="run.adapter_event",
            params={
                "run_id": context.run_id,
                "kind": kind,
                "status": self._normalize_adapter_event_status(status),
                "event_id": event_id,
                "message": message,
                "data": data or {},
            },
        )


__all__ = ["SessionFinishActions", "SessionLike"]
