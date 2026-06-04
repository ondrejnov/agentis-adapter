from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from common.config import Settings
from common.models import AgentExecutionContextPayload, completion_task_status


_SESSION_START_TIMEOUT_SEC = 300.0
_UNDERLYING_ADAPTERS = {"opencode", "oc", "claude", "claudecode", "claude-code", "cloud", "cc"}


@dataclass
class _AgentisCodeSession:
    session_id: str | None
    context: AgentExecutionContextPayload
    worktree: str
    proc: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None
    ready_event: threading.Event = field(default_factory=threading.Event)
    start_error: str | None = None


class AgentisCodeSessionManager:
    """Runs `agentiscode` CLI jobs and lets the CLI report telemetry to Agentis."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sessions: dict[str, _AgentisCodeSession] = {}
        self._lock = threading.Lock()

    def start(self, *, context: AgentExecutionContextPayload, worktree: str, prompt: str) -> str:
        sess = _AgentisCodeSession(session_id=None, context=context, worktree=worktree)
        self._spawn_thread(sess, prompt=prompt, resume_id=None)
        if not sess.ready_event.wait(timeout=_SESSION_START_TIMEOUT_SEC):
            self.abort_session(sess)
            raise RuntimeError(f"agentiscode session_id was not received within {_SESSION_START_TIMEOUT_SEC:.0f}s")
        if not sess.session_id:
            raise RuntimeError(sess.start_error or "agentiscode ended before reporting session_id")
        with self._lock:
            self._sessions[sess.session_id] = sess
        return sess.session_id

    def send(self, *, session_id: str, context: AgentExecutionContextPayload, worktree: str, prompt: str) -> None:
        sess = _AgentisCodeSession(session_id=session_id, context=context, worktree=worktree)
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

    def get_snapshot_key(self, session_id: str) -> None:
        return None

    def abort_session(self, sess: _AgentisCodeSession) -> None:
        proc = sess.proc
        if proc is None or proc.poll() is not None:
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
        proc = subprocess.Popen(
            args,
            cwd=sess.worktree,
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

        returncode = proc.wait()
        if not sess.ready_event.is_set():
            sess.start_error = f"agentiscode exited before session_id (exit_code={returncode})"
            sess.ready_event.set()

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
            "--last-message-to-comment",
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
        runtime = context.adapter.runtime.strip().lower() if context.adapter and context.adapter.runtime else ""
        if runtime in _UNDERLYING_ADAPTERS:
            return runtime
        return self.settings.agentiscode_adapter


__all__ = ["AgentisCodeSessionManager"]
