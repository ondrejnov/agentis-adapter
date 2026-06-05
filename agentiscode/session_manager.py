from __future__ import annotations

import json
import os
import signal
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

from common.cli_session import KubectlExecTarget
from common.config import Settings
from common.models import AgentExecutionContextPayload, completion_task_status
from common.session_manager import BaseSessionManager
from common.artifacts.expected import collect_expected_artifacts
from common.artifacts.screenshots import collect_screenshot_images
from common.artifacts.source_snapshot import (
    build_snapshot_key,
    changes_diff_attachment,
    snapshot_sources_best_effort,
    write_changes_diff_best_effort,
)


_SESSION_START_TIMEOUT_SEC = 300.0
_UNDERLYING_ADAPTERS = {"opencode", "oc", "claude", "claudecode", "claude-code", "cloud", "cc"}


@dataclass
class _AgentisCodeSession:
    session_id: str | None
    context: AgentExecutionContextPayload
    worktree: str
    kubectl_target: KubectlExecTarget | None = None
    proc: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None
    ready_event: threading.Event = field(default_factory=threading.Event)
    abort_event: threading.Event = field(default_factory=threading.Event)
    start_error: str | None = None
    snapshot_key: str | None = None
    final_text_chunks: list[str] = field(default_factory=list)
    final_text_open: bool = False
    is_error: bool = False


class AgentisCodeSessionManager:
    """Runs `agentiscode` CLI jobs and lets the CLI report telemetry to Agentis."""

    _AGENT_LABEL = "agentiscode"
    _completion_actions = staticmethod(BaseSessionManager._completion_actions)
    _normalize_adapter_event_status = staticmethod(BaseSessionManager._normalize_adapter_event_status)
    _commit_session_changes = BaseSessionManager._commit_session_changes
    _ensure_pull_request = BaseSessionManager._ensure_pull_request
    _run_completed_process = BaseSessionManager._run_completed_process
    _start_dev_server = BaseSessionManager._start_dev_server
    _finish_session_actions = BaseSessionManager._finish_session_actions
    _agentis_call = BaseSessionManager._agentis_call
    _emit_adapter_event = BaseSessionManager._emit_adapter_event

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sessions: dict[str, _AgentisCodeSession] = {}
        self._lock = threading.Lock()

    def start(
        self,
        *,
        context: AgentExecutionContextPayload,
        worktree: str,
        prompt: str,
        kubectl_target: KubectlExecTarget | None = None,
    ) -> str:
        sess = _AgentisCodeSession(session_id=None, context=context, worktree=worktree, kubectl_target=kubectl_target)
        pending_key = f"agentiscode-pending-{uuid4().hex}"
        sess.snapshot_key = build_snapshot_key(self._AGENT_LABEL, context.run_id, context.task_id, pending_key)
        snapshot_sources_best_effort(worktree, sess.snapshot_key, label="agentiscode-start")
        self._spawn_thread(sess, prompt=prompt, resume_id=None)
        if not sess.ready_event.wait(timeout=_SESSION_START_TIMEOUT_SEC):
            self.abort_session(sess)
            raise RuntimeError(f"agentiscode session_id was not received within {_SESSION_START_TIMEOUT_SEC:.0f}s")
        if not sess.session_id:
            raise RuntimeError(sess.start_error or "agentiscode ended before reporting session_id")
        with self._lock:
            self._sessions[sess.session_id] = sess
        return sess.session_id

    def send(
        self,
        *,
        session_id: str,
        context: AgentExecutionContextPayload,
        worktree: str,
        prompt: str,
        kubectl_target: KubectlExecTarget | None = None,
    ) -> None:
        sess = _AgentisCodeSession(
            session_id=session_id,
            context=context,
            worktree=worktree,
            kubectl_target=kubectl_target,
        )
        sess.snapshot_key = build_snapshot_key(
            self._AGENT_LABEL, context.run_id, context.task_id, session_id, uuid4().hex
        )
        snapshot_sources_best_effort(worktree, sess.snapshot_key, label="agentiscode-send")
        with self._lock:
            self._sessions[session_id] = sess
        self._spawn_thread(sess, prompt=prompt, resume_id=session_id)

    def abort(self, session_id: str) -> None:
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is not None:
            self.abort_session(sess)

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def get_snapshot_key(self, session_id: str) -> str | None:
        with self._lock:
            sess = self._sessions.get(session_id)
        return sess.snapshot_key if sess is not None else None

    def abort_session(self, sess: _AgentisCodeSession) -> None:
        sess.abort_event.set()
        proc = sess.proc
        if proc is None or proc.poll() is not None:
            return
        if sess.kubectl_target is not None:
            try:
                proc.kill()
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"[agentiscode-session] kill kubectl client failed: {exc}\n")
            self._remote_pkill_agentiscode(sess.kubectl_target)
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[agentiscode-session] killpg failed: {exc}\n")
        try:
            proc.kill()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[agentiscode-session] kill failed: {exc}\n")

    def _spawn_thread(self, sess: _AgentisCodeSession, *, prompt: str, resume_id: str | None) -> None:
        thread_id = sess.session_id or f"pending-{uuid4().hex}"
        thread = threading.Thread(
            target=self._thread_main,
            args=(sess, prompt, resume_id),
            name=f"agentiscode-session-{thread_id}",
            daemon=True,
        )
        sess.thread = thread
        thread.start()

    def _thread_main(self, sess: _AgentisCodeSession, prompt: str, resume_id: str | None) -> None:
        try:
            self._run_process(sess, prompt, resume_id)
        except Exception as exc:  # noqa: BLE001
            sess.start_error = str(exc)
            sess.ready_event.set()
            sys.stderr.write(f"[agentiscode-session] crashed: {exc!r}\n")

    def _run_process(self, sess: _AgentisCodeSession, prompt: str, resume_id: str | None) -> None:
        args = self._build_args(sess.context, sess.worktree, prompt, resume_id)
        cwd = sess.worktree
        if sess.kubectl_target is not None:
            args = self._build_kubectl_args(sess.kubectl_target, sess.worktree, args)
            cwd = None
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        sess.proc = proc
        stderr_thread = threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True)
        stderr_thread.start()

        assert proc.stdout is not None
        for line in proc.stdout:
            payload = self._parse_json_line(line)
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "session" and not sess.session_id:
                session_id = payload.get("session_id")
                if isinstance(session_id, str) and session_id:
                    sess.session_id = session_id
                    sess.ready_event.set()
            self._consume_output_event(sess, payload)

        returncode = proc.wait()
        if returncode != 0:
            sess.is_error = True
        if not sess.ready_event.is_set():
            sess.start_error = f"agentiscode exited before session_id (exit_code={returncode})"
            sess.ready_event.set()
        self._finish_agentiscode_session(sess)

    @staticmethod
    def _consume_output_event(sess: _AgentisCodeSession, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        raw_data = payload.get("data")
        data = raw_data if isinstance(raw_data, dict) else payload
        if event_type == "text":
            text = data.get("text")
            if isinstance(text, str) and text:
                if not sess.final_text_open:
                    sess.final_text_chunks.clear()
                sess.final_text_chunks.append(text)
                sess.final_text_open = True
        elif event_type in {"reasoning", "tool"}:
            sess.final_text_open = False
        elif event_type == "error" or (event_type == "result" and data.get("is_error")):
            sess.is_error = True

    def _finish_agentiscode_session(self, sess: _AgentisCodeSession) -> None:
        if sess.abort_event.is_set() or not sess.session_id:
            return

        try:
            attachments = self._finish_session_actions(cast(Any, sess), sess.session_id)
            if sess.snapshot_key:
                diff_result = write_changes_diff_best_effort(
                    sess.worktree,
                    sess.snapshot_key,
                    label="agentiscode-finish",
                )
                diff_attachment = changes_diff_attachment(diff_result)
                if diff_attachment:
                    attachments.append(diff_attachment)

            body = "".join(sess.final_text_chunks).strip()
            if body:
                params: dict[str, Any] = {
                    "session_id": sess.session_id,
                    "body": body,
                    "attachments": attachments,
                    "images": collect_screenshot_images(sess.worktree),
                    "artifacts": collect_expected_artifacts(sess.context, sess.worktree),
                    "status": completion_task_status(sess.context),
                    "comment_type": "primary",
                    "actions": self._completion_actions(sess.context),
                }
                self._agentis_call(method="task.add_agent_comment", params=params)
        finally:
            self._emit_adapter_event(
                sess.context,
                kind="idle",
                status="failed" if sess.is_error else "success",
                event_id=f"idle:{sess.session_id}:agentiscode-finish",
                message="agentiscode session selhala." if sess.is_error else "agentiscode session doběhla.",
                data={"session_id": sess.session_id},
            )

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen[str]) -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    @staticmethod
    def _parse_json_line(line: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _build_kubectl_args(target: KubectlExecTarget, worktree: str, args: list[str]) -> list[str]:
        if shutil.which(target.kubectl) is None and not os.path.isabs(target.kubectl):
            raise RuntimeError(f"kubectl CLI is not available on PATH: {target.kubectl}")
        inner = " ".join(shlex.quote(arg) for arg in args)
        if worktree:
            inner = f"cd {shlex.quote(worktree)} && exec {inner}"
        kubectl_args = [target.kubectl, "-n", target.namespace, "exec", "-i", target.selector]
        if target.container:
            kubectl_args.extend(["-c", target.container])
        kubectl_args.extend(["--", "sh", "-c", inner])
        return kubectl_args

    @staticmethod
    def _remote_pkill_agentiscode(target: KubectlExecTarget) -> None:
        if shutil.which(target.kubectl) is None and not os.path.isabs(target.kubectl):
            sys.stderr.write(f"[agentiscode-session] remote pkill skipped: kubectl not on PATH ({target.kubectl})\n")
            return
        args = [target.kubectl, "-n", target.namespace, "exec", target.selector]
        if target.container:
            args.extend(["-c", target.container])
        args.extend(["--", "pkill", "-KILL", "-f", "agentiscode"])
        try:
            subprocess.run(args, capture_output=True, text=True, timeout=15.0, check=False)
        except subprocess.TimeoutExpired:
            sys.stderr.write("[agentiscode-session] remote pkill timed out\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[agentiscode-session] remote pkill failed: {exc}\n")

    def _build_args(
        self,
        context: AgentExecutionContextPayload,
        worktree: str,
        prompt: str,
        resume_id: str | None,
    ) -> list[str]:
        if not self.settings.agentis_endpoint:
            raise RuntimeError("AGENTIS_ENDPOINT is required for agentiscode telemetry")

        adapter_opts = context.adapter
        underlying_adapter = self._underlying_adapter(context)
        args = [
            self.settings.agentiscode_command,
            "--adapter",
            underlying_adapter,
            "--cwd",
            worktree,
            "--json",
            "--task-id",
            context.task_id,
            "--run-id",
            context.run_id,
            "--task-status",
            str(completion_task_status(context)),
            "--agentis-api",
            self.settings.agentis_endpoint,
        ]
        if self.settings.agentis_token:
            args.extend(["--agentis-token", self.settings.agentis_token])
        if adapter_opts and adapter_opts.model:
            args.extend(["--model", adapter_opts.model])
        if adapter_opts and adapter_opts.variant:
            args.extend(["--effort", adapter_opts.variant])
        if adapter_opts and adapter_opts.agent:
            args.extend(["--agent", adapter_opts.agent])
        if resume_id:
            args.extend(["--resume", resume_id])
        args.append(prompt)
        return args

    def _underlying_adapter(self, context: AgentExecutionContextPayload) -> str:
        if context.adapter and context.adapter.runtime:
            runtime = context.adapter.runtime.strip().lower()
            if runtime in {"claude", "claudecode", "claude-code", "cloud", "cc"}:
                return "claude"
            if runtime in {"opencode", "oc"}:
                return "opencode"
        if context.adapter and context.adapter.model and "claude" in context.adapter.model:
            return "claude"
        return "opencode"


__all__ = ["AgentisCodeSessionManager"]
