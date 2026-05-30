"""Agent-agnostic background runner for single-prompt CLI agent sessions.

Pro každou agentí session držíme jeden řídicí thread, který asynchronně
streamuje výstup z CLI agenta (Claude Code, OpenCode run, …) a postupně
forwarduje aktivitu do Agentisu (``session.store_activity_log``,
``task.add_agent_comment``, ``run.adapter_event``).

Celá orchestrace (streaming, activity-log forwarding, dokončovací akce) je
agnostická vůči konkrétnímu agentovi. Agentně specifické chování se injektuje
přes pár hooků (``_AGENT_LABEL``, ``_REMOTE_PKILL_PATTERN``, ``_make_mapper``,
``_build_client``), takže jednotliví single-prompt CLI agenti pouze podědí
``BaseSessionManager`` a přepíšou tyto hooky.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from uuid import uuid4

from common.config import Settings
from common.models import AgentExecutionContextPayload, completion_task_status
from common.kubernetes_runtime import KubernetesAdapterService
from common.artifacts.expected import collect_expected_artifacts
from common.artifacts.screenshots import collect_screenshot_images
from common.artifacts.source_snapshot import (
    build_snapshot_key,
    changes_diff_attachment,
    snapshot_sources_best_effort,
    write_changes_diff_best_effort,
)
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.integrations.github_pr import GithubPrError, GithubPrResult, GithubPrService

if TYPE_CHECKING:
    from common.cli_session import KubectlExecTarget


_AGENT_SESSION_START_TIMEOUT_SEC = 15.0
_ALLOWED_ADAPTER_EVENT_STATUSES = {"started", "success", "failed"}


@dataclass
class _AgentSession:
    session_id: Optional[str]
    pending_key: str
    context: AgentExecutionContextPayload
    worktree: str
    kubectl_target: Optional["KubectlExecTarget"] = None
    agent_session_id: Optional[str] = None
    abort_event: threading.Event = field(default_factory=threading.Event)
    proc_holder: dict[str, Any] = field(default_factory=dict)  # {"proc": asyncio.subprocess.Process}
    thread: Optional[threading.Thread] = None
    ready_event: threading.Event = field(default_factory=threading.Event)
    start_error: Optional[str] = None
    snapshot_key: Optional[str] = None


class BaseSessionManager:
    """Owns background CLI agent runs keyed by the real agent session_id.

    The orchestration (streaming, activity-log forwarding, completion actions)
    is agent-agnostic. Agent-specific behavior is injected through a handful of
    hooks (``_AGENT_LABEL``, ``_REMOTE_PKILL_PATTERN``, ``_make_mapper``,
    ``_build_client``) so each single-prompt CLI agent (Claude Code, OpenCode
    run, …) can reuse the same lifecycle by subclassing.
    """

    # Prefix used for snapshot keys / adapter event kinds and the label shown
    # in source-snapshot records. Overridden by each concrete agent.
    _AGENT_LABEL = "agent"
    # `pkill -f` pattern used to terminate the agent process inside a k8s pod.
    _REMOTE_PKILL_PATTERN = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sessions: dict[str, _AgentSession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        context: AgentExecutionContextPayload,
        worktree: str,
        prompt: str,
        kubectl_target: Optional["KubectlExecTarget"] = None,
    ) -> str:
        """Spustí novou session a vrátí skutečné agentí session_id."""
        mode = self._mode_from_context(context)
        mapper = self._make_mapper(prompt=prompt, mode=mode, cwd=worktree)
        pending_key = f"{self._AGENT_LABEL}-pending-{uuid4().hex}"
        sess = _AgentSession(
            session_id=None,
            pending_key=pending_key,
            context=context,
            worktree=worktree,
            kubectl_target=kubectl_target,
        )
        sess.snapshot_key = build_snapshot_key(self._AGENT_LABEL, context.run_id, context.task_id, pending_key)
        snapshot_sources_best_effort(worktree, sess.snapshot_key, label=f"{self._AGENT_LABEL}-start")
        with self._lock:
            self._sessions[pending_key] = sess

        self._spawn_thread(sess, prompt=prompt, mapper=mapper, resume_id=None)
        if not sess.ready_event.wait(timeout=_AGENT_SESSION_START_TIMEOUT_SEC):
            self.abort(pending_key)
            with self._lock:
                self._sessions.pop(pending_key, None)
            raise RuntimeError(
                f"{self._AGENT_LABEL} session_id was not received within {_AGENT_SESSION_START_TIMEOUT_SEC:.0f}s"
            )

        if not sess.session_id:
            with self._lock:
                self._sessions.pop(pending_key, None)
            raise RuntimeError(sess.start_error or f"{self._AGENT_LABEL} session ended before reporting session_id")

        return sess.session_id

    def send(
        self,
        session_id: str,
        context: AgentExecutionContextPayload,
        worktree: str,
        prompt: str,
        kubectl_target: Optional["KubectlExecTarget"] = None,
    ) -> None:
        """Pošle do existující session další prompt (nový run s `--resume`)."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            raise RuntimeError(f"Unknown {self._AGENT_LABEL} session {session_id!r}")

        sess.context = context
        sess.worktree = worktree
        if kubectl_target is not None:
            sess.kubectl_target = kubectl_target
        sess.abort_event = threading.Event()
        sess.snapshot_key = build_snapshot_key(
            self._AGENT_LABEL, context.run_id, context.task_id, session_id, uuid4().hex
        )
        snapshot_sources_best_effort(worktree, sess.snapshot_key, label=f"{self._AGENT_LABEL}-send")

        mode = self._mode_from_context(context)
        mapper = self._make_mapper(
            prompt=prompt,
            mode=mode,
            cwd=worktree,
            session_id_hint=sess.agent_session_id,
        )
        self._spawn_thread(sess, prompt=prompt, mapper=mapper, resume_id=sess.agent_session_id)

    def abort(self, session_id: str) -> None:
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            return
        sess.abort_event.set()
        proc = sess.proc_holder.get("proc")
        kubectl_target = sess.kubectl_target

        if kubectl_target is not None:
            # `proc` je lokální `kubectl exec` klient — jeho zabití pouze
            # přeruší stream, ale agentí proces uvnitř podu běží dál.
            # Zabijeme tedy agenta přímo v podu pomocí samostatného
            # `kubectl exec ... -- pkill`.
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(
                        f"[{self._AGENT_LABEL}-session] kill kubectl client failed for {session_id}: {exc}\n"
                    )
            self._remote_pkill_agent(kubectl_target, session_id)
            return

        if proc is None:
            return

        # Lokální mód — proces byl spuštěn s `start_new_session=True`,
        # takže mu patří vlastní process group. Killneme celou skupinu,
        # aby šly s CLI agentem dolů i jeho potomci.
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        except OSError as exc:
            sys.stderr.write(f"[{self._AGENT_LABEL}-session] getpgid failed for {session_id}: {exc}\n")
            pgid = None

        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return
            except OSError as exc:
                sys.stderr.write(f"[{self._AGENT_LABEL}-session] killpg failed for {session_id}: {exc}\n")

        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[{self._AGENT_LABEL}-session] kill failed for {session_id}: {exc}\n")

    def _remote_pkill_agent(self, target: "KubectlExecTarget", session_id: str) -> None:
        kubectl_path = target.kubectl
        if shutil.which(kubectl_path) is None and not os.path.isabs(kubectl_path):
            sys.stderr.write(
                f"[{self._AGENT_LABEL}-session] remote pkill skipped for {session_id}: "
                f"kubectl not on PATH ({kubectl_path})\n"
            )
            return

        args = [kubectl_path, "-n", target.namespace, "exec", target.selector]
        if target.container:
            args.extend(["-c", target.container])
        args.extend(["--", "pkill", "-KILL", "-f", self._REMOTE_PKILL_PATTERN])
        try:
            subprocess.run(args, capture_output=True, text=True, timeout=15.0, check=False)
        except subprocess.TimeoutExpired:
            sys.stderr.write(f"[{self._AGENT_LABEL}-session] remote pkill timed out for {session_id}\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[{self._AGENT_LABEL}-session] remote pkill failed for {session_id}: {exc}\n")

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _bind_session_id(self, sess: _AgentSession, session_id: str) -> None:
        is_new_session = sess.session_id is None
        with self._lock:
            self._sessions.pop(sess.pending_key, None)
            if sess.session_id and sess.session_id != session_id:
                self._sessions.pop(sess.session_id, None)
            sess.session_id = session_id
            sess.agent_session_id = session_id
            self._sessions[session_id] = sess
        sess.ready_event.set()
        if is_new_session:
            self._emit_session_created(sess, session_id)

    def _emit_session_created(self, sess: _AgentSession, session_id: str) -> None:
        self._agentis_call(
            method="session.session_created",
            params={
                "session": {
                    "id": session_id,
                    "parentID": None,
                    "title": sess.context.title,
                },
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _mode_from_context(context: AgentExecutionContextPayload) -> str:
        if context.adapter and context.adapter.agent:
            return context.adapter.agent
        return "build"

    # ------------------------------------------------------------------
    # Agent-specific hooks (implemented by each concrete CLI agent)
    # ------------------------------------------------------------------

    def _make_mapper(
        self,
        *,
        prompt: str,
        mode: str,
        cwd: str,
        session_id_hint: Optional[str] = None,
    ) -> Any:
        raise NotImplementedError

    def _build_client(self, sess: _AgentSession, resume_id: Optional[str]) -> Any:
        raise NotImplementedError

    def _spawn_thread(
        self,
        sess: _AgentSession,
        *,
        prompt: str,
        mapper: Any,
        resume_id: Optional[str],
    ) -> None:
        thread_session_id = sess.session_id or sess.pending_key
        thread = threading.Thread(
            target=self._thread_main,
            args=(sess, prompt, mapper, resume_id),
            name=f"{self._AGENT_LABEL}-session-{thread_session_id}",
            daemon=True,
        )
        sess.thread = thread
        thread.start()

    def _thread_main(
        self,
        sess: _AgentSession,
        prompt: str,
        mapper: Any,
        resume_id: Optional[str],
    ) -> None:
        try:
            asyncio.run(self._stream(sess, prompt, mapper, resume_id))
        except Exception as exc:  # noqa: BLE001
            session_ref = sess.session_id or sess.pending_key
            sess.start_error = str(exc)
            sess.ready_event.set()
            sys.stderr.write(f"[{self._AGENT_LABEL}-session] {session_ref} crashed: {exc!r}\n")
            self._emit_adapter_event(
                sess.context,
                kind=f"{self._AGENT_LABEL}_run",
                status="failed",
                event_id=f"{self._AGENT_LABEL}_run:{session_ref}:{uuid4().hex}",
                message="CLI běh agenta selhal.",
                data={"error": str(exc)},
            )

    async def _stream(
        self,
        sess: _AgentSession,
        prompt: str,
        mapper: Any,
        resume_id: Optional[str],
    ) -> None:
        run_session_id = sess.session_id or sess.pending_key
        client = self._build_client(sess, resume_id)

        run_event_id = f"{self._AGENT_LABEL}_run:{run_session_id}:{uuid4().hex}"

        def _register_proc(proc: asyncio.subprocess.Process) -> None:
            sess.proc_holder["proc"] = proc

        last_pushed_len = 0
        try:
            async for event in client.stream(prompt=prompt, on_proc_started=_register_proc):
                if sess.abort_event.is_set():
                    break

                if event.type == "session_start" and client.session_id:
                    self._bind_session_id(sess, client.session_id)

                if event.type == "error" and not sess.ready_event.is_set():
                    sess.start_error = event.data.get("message") or "CLI agent failed"
                    sess.ready_event.set()

                changed = mapper.consume(event)

                if changed and sess.session_id:
                    snapshot = mapper.snapshot()
                    last_pushed_len = len(snapshot)
                    self._agentis_call(
                        method="session.store_activity_log",
                        params={"session_id": sess.session_id, "messages": snapshot},
                    )
        finally:
            if not sess.ready_event.is_set():
                sess.start_error = sess.start_error or f"{self._AGENT_LABEL} session ended before reporting session_id"
                sess.ready_event.set()

            snapshot = mapper.snapshot()
            if sess.session_id and len(snapshot) != last_pushed_len:
                self._agentis_call(
                    method="session.store_activity_log",
                    params={"session_id": sess.session_id, "messages": snapshot},
                )

            body = self._extract_final_text(snapshot)
            attachments: list[dict[str, Any]] = []
            if not sess.abort_event.is_set():
                attachments = self._finish_session_actions(sess, sess.session_id or run_session_id)
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

            self._emit_adapter_event(
                sess.context,
                kind=f"{self._AGENT_LABEL}_idle",
                status="success" if not sess.abort_event.is_set() else "failed",
                event_id=run_event_id,
                message=("Session byla zastavena." if sess.abort_event.is_set() else "Session doběhla."),
                data={
                    "session_id": sess.session_id,
                    "agent_session_id": sess.agent_session_id,
                    "cost_usd": client.last_cost_usd,
                    "usage": client.last_usage,
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

    def _ensure_pull_request(
        self,
        context: AgentExecutionContextPayload,
        worktree_path: Path,
    ) -> GithubPrResult | None:
        if KubernetesAdapterService.is_project_scope(context):
            return None
        if not context.project_github_repo:
            return None

        try:
            branch = KubernetesAdapterService._branch_name_for_context(context)
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

    def _start_dev_server(self, sess: _AgentSession) -> dict[str, Any]:
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

    def _finish_session_actions(self, sess: _AgentSession, session_ref: str) -> list[dict[str, Any]]:
        context = sess.context
        if KubernetesAdapterService.is_project_scope(context) or not context.project_github_repo:
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
