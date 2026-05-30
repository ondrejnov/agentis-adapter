from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError, AgentisRunLogger


def log_json(level: str, message: str, **fields) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
        **fields,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class BaseAdapterService:
    DEFAULT_WORKTREE_DIRNAME = "worktree"
    requires_agentis_init = False

    def __init__(self, context: AgentExecutionContextPayload, settings: Settings):
        self.context = context
        self.settings = settings
        print(f"Adapter initialized with context: {self.context}")

    @staticmethod
    def _run_git(cwd: Path, *args: str) -> str:
        print(args)
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
            command = " ".join(["git", "-C", str(cwd), *args])
            raise RuntimeError(f"{command} failed: {stderr}")
        return completed.stdout.strip()

    @staticmethod
    def _git_succeeds(cwd: Path, *args: str) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0

    @staticmethod
    def _task_safe_name(task_id: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id.strip())
        sanitized = re.sub(r"-{2,}", "-", sanitized).strip(".-")
        if not sanitized:
            raise RuntimeError("task_id cannot be converted to a git-safe name")
        if sanitized.endswith(".lock"):
            sanitized = f"{sanitized[:-5]}-lock"
        return sanitized

    @classmethod
    def _branch_name_for_task(cls, task_id: str) -> str:
        task_name = cls._task_safe_name(task_id)
        return task_name if task_name.startswith("task-") else f"task-{task_name}"

    @classmethod
    def _branch_name_for_context(cls, context: AgentExecutionContextPayload) -> str:
        if context.adapter and context.adapter.branch:
            return context.adapter.branch
        return cls._branch_name_for_task(context.task_id)

    @staticmethod
    def is_project_scope(context: AgentExecutionContextPayload) -> bool:
        return bool(context.adapter and context.adapter.scope == "project")

    @staticmethod
    def _kubernetes_safe_name(value: str) -> str:
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

    def _repository_root(self) -> Path:
        if not self.context.working_dir:
            raise RuntimeError("working_dir is required")
        repository_root = self._run_git(Path(self.context.working_dir), "rev-parse", "--show-toplevel")
        return Path(repository_root)

    def _resolve_base_ref(self, repository_root: Path) -> str:
        candidates = [self.context.base_branch]
        if "/" not in self.context.base_branch:
            candidates.append(f"origin/{self.context.base_branch}")

        for candidate in candidates:
            if self._git_succeeds(
                repository_root,
                "rev-parse",
                "--verify",
                "--quiet",
                f"{candidate}^{{commit}}",
            ):
                return candidate

        raise RuntimeError(f"Base branch {self.context.base_branch!r} was not found")

    def _resolve_push_remote(self, repository_root: Path) -> str:
        if self._git_succeeds(repository_root, "config", "--get", f"branch.{self.context.base_branch}.remote"):
            remote = self._run_git(repository_root, "config", "--get", f"branch.{self.context.base_branch}.remote")
            if remote:
                return remote

        remotes = [line.strip() for line in self._run_git(repository_root, "remote").splitlines() if line.strip()]
        if "origin" in remotes:
            return "origin"
        if len(remotes) == 1:
            return remotes[0]

        raise RuntimeError("Could not determine git remote for base branch push")

    def _default_worktree_path(self, repository_root: Path) -> Path:
        base = self.settings.worktree_root or repository_root.parent / self.DEFAULT_WORKTREE_DIRNAME
        return base / self._task_safe_name(self.context.task_id)

    def _resolved_worktree_path(self) -> Path:
        return self.settings.worktree_root / self._task_safe_name(self.context.task_id)

    def _workspace_path(self) -> Path:
        if self.is_project_scope(self.context):
            return self._repository_root()
        return self._resolved_worktree_path()

    def create_worktree(self) -> dict[str, str | None]:
        repository_root = self._repository_root()
        print(f"Repository root: {repository_root}")
        if self.is_project_scope(self.context):
            current_branch = self._run_git(repository_root, "branch", "--show-current") or None
            return {
                "action": "create_worktree",
                "task_id": self.context.task_id,
                "branch": current_branch,
                "base_branch": self.context.base_branch,
                "working_dir": str(repository_root),
                "status": "skipped",
                "reason": "project_scope",
            }

        branch_name = self._branch_name_for_context(self.context)
        print(f"Branch name for task: {branch_name}")
        worktree_path = self._resolved_worktree_path()
        print(f"Resolved worktree path: {worktree_path}")

        log_json(
            "INFO",
            "Creating git worktree",
            task_id=self.context.task_id,
            project_slug=self.context.project_slug,
            working_dir=str(worktree_path),
            branch=branch_name,
            base_branch=self.context.base_branch,
            repository_root=str(repository_root),
        )

        if worktree_path == repository_root or repository_root in worktree_path.parents:
            raise RuntimeError("working_dir must be outside the source repository worktree")

        if worktree_path.exists() and self._git_succeeds(worktree_path, "rev-parse", "--is-inside-work-tree"):
            existing_root = Path(self._run_git(worktree_path, "rev-parse", "--show-toplevel"))
            if existing_root != worktree_path:
                raise RuntimeError(f"working_dir {worktree_path} is inside an existing git worktree")

            current_branch = self._run_git(worktree_path, "branch", "--show-current")
            if current_branch != branch_name:
                raise RuntimeError(
                    f"working_dir {worktree_path} already exists on branch {current_branch}, expected {branch_name}"
                )

            return {
                "action": "create_worktree",
                "task_id": self.context.task_id,
                "branch": branch_name,
                "base_branch": self.context.base_branch,
                "working_dir": str(worktree_path),
                "status": "reused",
            }

        if worktree_path.exists() and any(worktree_path.iterdir()):
            raise RuntimeError(f"working_dir {worktree_path} already exists and is not empty")

        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        branch_exists = self._git_succeeds(
            repository_root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch_name}",
        )
        if branch_exists:
            self._run_git(repository_root, "worktree", "add", str(worktree_path), branch_name)
            status = "attached"
        else:
            base_ref = self._resolve_base_ref(repository_root)
            self._run_git(
                repository_root,
                "worktree",
                "add",
                "-b",
                branch_name,
                str(worktree_path),
                base_ref,
            )
            status = "created"

        return {
            "action": "create_worktree",
            "task_id": self.context.task_id,
            "branch": branch_name,
            "base_branch": self.context.base_branch,
            "working_dir": str(worktree_path),
            "status": status,
        }

    def _call_agentis_rpc(self, method: str, params: dict[str, Any], *, timeout: float = 10.0) -> Any:
        endpoint = self.settings.agentis_endpoint
        if not endpoint:
            raise RuntimeError("agentis_endpoint is not configured")

        try:
            with self._agentis_client_class()(
                endpoint=endpoint,
                token=self.settings.agentis_token,
                timeout=timeout,
            ) as client:
                return client.call(method=method, params=params, request_id=1)
        except AgentisJsonRpcError as exc:
            raise RuntimeError(str(exc)) from exc

    def _agentis_client_class(self) -> Any:
        return AgentisJsonRpcClient

    def post_agentis_event(
        self,
        *,
        kind: str,
        status: str,
        event_id: str | None = None,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if not self.settings.agentis_endpoint:
            log_json(
                "WARN",
                "Agentis endpoint missing; skipping adapter event",
                task_id=self.context.task_id,
                run_id=self.context.run_id,
                kind=kind,
                status=status,
            )
            return

        normalized_event_id = event_id or f"{kind}:{uuid4().hex}"
        payload = {
            "run_id": self.context.run_id,
            "kind": kind,
            "status": status,
            "event_id": normalized_event_id,
            "message": message,
            "data": data or {},
        }
        log_json(
            "INFO",
            "Posting adapter event to Agentis",
            task_id=self.context.task_id,
            run_id=self.context.run_id,
            kind=kind,
            status=status,
            event_id=normalized_event_id,
            event_message=message,
        )
        try:
            self._call_agentis_rpc("run.adapter_event", payload)
        except Exception as exc:
            print(f"Failed to post adapter event to Agentis: {exc}", file=sys.stderr)
            log_json(
                "WARN",
                "Failed to post adapter event to Agentis",
                task_id=self.context.task_id,
                run_id=self.context.run_id,
                kind=kind,
                status=status,
                event_id=normalized_event_id,
                error=str(exc),
            )

    def _persist_agentis_session_id(self, session_id: str) -> None:
        if not self.settings.agentis_endpoint:
            log_json(
                "WARN",
                "Agentis endpoint missing; skipping session persistence",
                task_id=self.context.task_id,
                run_id=self.context.run_id,
                session_id=session_id,
            )
            return

        log_json(
            "INFO",
            "Persisting adapter session in Agentis",
            task_id=self.context.task_id,
            run_id=self.context.run_id,
            session_id=session_id,
        )
        try:
            self._call_agentis_rpc(
                "run.store_session_id",
                {
                    "run_id": self.context.run_id,
                    "session_id": session_id,
                },
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to persist adapter session for run {self.context.run_id}: {exc}") from exc

        log_json(
            "INFO",
            "Adapter session persisted in Agentis",
            task_id=self.context.task_id,
            run_id=self.context.run_id,
            session_id=session_id,
        )

    def deploy(self) -> dict[str, Any]:
        raise NotImplementedError

    def wait_ready(self, timeout: float = 300.0, interval: float = 2.0) -> dict[str, Any]:
        raise NotImplementedError

    def start_session(self, pod_url: str | None = None, fork_from_session_id: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def add_message(self, message: str, pod_url: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def question_reply(
        self,
        request_id: str,
        answers: list[list[str]],
        pod_url: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def abort(self, session_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def git_merge(self, message: str | None = None) -> dict[str, str | None]:
        """Rebase the task branch and fast-forward it into the project's base branch."""
        del message
        repository_root = self._repository_root()
        if self.is_project_scope(self.context):
            current_branch = self._run_git(repository_root, "branch", "--show-current") or None
            return {
                "action": "git_merge",
                "task_id": self.context.task_id,
                "branch": current_branch,
                "base_branch": self.context.base_branch,
                "status": "skipped",
                "reason": "project_scope",
                "repository_root": str(repository_root),
            }

        branch_name = self._branch_name_for_context(self.context)
        base_branch = self.context.base_branch
        worktree_path = self._resolved_worktree_path()

        log_json(
            "INFO",
            "Rebasing task branch and fast-forwarding base branch",
            task_id=self.context.task_id,
            branch=branch_name,
            base_branch=base_branch,
            repository_root=str(repository_root),
        )

        if not self._git_succeeds(
            repository_root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch_name}",
        ):
            raise RuntimeError(f"Branch {branch_name!r} does not exist in {repository_root}")

        if not self._git_succeeds(worktree_path, "rev-parse", "--is-inside-work-tree"):
            raise RuntimeError(f"Task worktree {worktree_path} does not exist")

        worktree_branch = self._run_git(worktree_path, "branch", "--show-current")
        if worktree_branch != branch_name:
            raise RuntimeError(f"Task worktree {worktree_path} is on branch {worktree_branch}, expected {branch_name}")

        current_branch = self._run_git(repository_root, "branch", "--show-current")
        previous_branch = current_branch or None

        push_remote = self._resolve_push_remote(repository_root)
        remote_base_ref = f"refs/remotes/{push_remote}/{base_branch}"
        conflict_resolution_output: str | None = None
        with AgentisRunLogger(run_id=self.context.run_id) as log:
            try:
                self._run_git(repository_root, "fetch", push_remote, base_branch)
                try:
                    self._run_git(worktree_path, "rebase", remote_base_ref)
                except RuntimeError:
                    log.started("git-merge-agent", message="Spouštím git merge AI agenta", event_id="1")
                    opencode_output = subprocess.run(
                        ["/usr/bin/opencode", "run", "--model", "openai/gpt-5.4", "fix git conflict"],
                        cwd=worktree_path,
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout
                    conflict_resolution_output = opencode_output
                    print(opencode_output)
                    log.success("git-merge-agent", message=opencode_output, event_id="1")
                    try:
                        self._run_git(worktree_path, "-c", "core.editor=true", "rebase", "--continue")
                    except RuntimeError as error:
                        log.failed("git merge retry", message=str(error), event_id="1")
                        self._git_succeeds(worktree_path, "rebase", "--abort")
                        raise

                # self._run_git(repository_root, "checkout", base_branch)
                try:
                    self._run_git(repository_root, "rebase", branch_name)
                except RuntimeError as error:
                    if "You have unstaged changes" not in str(error):
                        raise
                    self._run_git(repository_root, "stash", "push")
                    self._run_git(repository_root, "rebase", branch_name)
                    self._run_git(repository_root, "stash", "pop")

                commit = self._run_git(repository_root, "rev-parse", "HEAD")
                self._run_git(
                    repository_root,
                    "push",
                    push_remote,
                    f"{base_branch}:refs/heads/{base_branch}",
                )
            finally:
                if previous_branch and previous_branch != base_branch:
                    self._git_succeeds(repository_root, "checkout", previous_branch)

        result: dict[str, str | None] = {
            "action": "git_merge",
            "task_id": self.context.task_id,
            "branch": branch_name,
            "base_branch": base_branch,
            "merge_commit": commit,
            "commit": commit,
            "push_remote": push_remote,
            "repository_root": str(repository_root),
        }
        if conflict_resolution_output is not None:
            result["conflict_resolution_output"] = conflict_resolution_output
        return result

    def _remove_worktree(
        self,
        repository_root: Path,
        worktree_path: Path,
        *,
        missing_is_removed: bool = False,
    ) -> bool:
        if worktree_path.exists():
            try:
                self._run_git(
                    repository_root,
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                )
                return True
            except RuntimeError as error:
                log_json(
                    "WARN",
                    "git worktree remove failed; removing worktree directory directly",
                    task_id=self.context.task_id,
                    worktree_path=str(worktree_path),
                    error=str(error),
                )
                try:
                    shutil.rmtree(worktree_path)
                except FileNotFoundError:
                    pass
                except OSError as cleanup_error:
                    log_json(
                        "WARN",
                        "Failed to remove worktree directory directly",
                        task_id=self.context.task_id,
                        worktree_path=str(worktree_path),
                        error=str(cleanup_error),
                    )
                self._git_succeeds(repository_root, "worktree", "prune")
                return not worktree_path.exists()

        self._git_succeeds(repository_root, "worktree", "prune")
        return missing_is_removed

    def _delete_branch(self, repository_root: Path, branch_name: str) -> bool:
        if not self._git_succeeds(
            repository_root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch_name}",
        ):
            return False
        self._run_git(repository_root, "branch", "-D", branch_name)
        return True

    def _cleanup_worktree_branch(
        self,
        repository_root: Path,
        branch_name: str,
        worktree_path: Path,
        *,
        missing_worktree_is_removed: bool = False,
    ) -> tuple[bool, bool]:
        worktree_removed = self._remove_worktree(
            repository_root,
            worktree_path,
            missing_is_removed=missing_worktree_is_removed,
        )
        branch_deleted = self._delete_branch(repository_root, branch_name)
        return worktree_removed, branch_deleted

    def close(self) -> dict[str, Any]:
        repository_root = self._repository_root()
        if self.is_project_scope(self.context):
            current_branch = self._run_git(repository_root, "branch", "--show-current") or None
            return {
                "action": "close",
                "task_id": self.context.task_id,
                "branch": current_branch,
                "base_branch": self.context.base_branch,
                "status": "skipped",
                "reason": "project_scope",
                "repository_root": str(repository_root),
                "worktree_removed": False,
                "branch_deleted": False,
            }

        branch_name = self._branch_name_for_context(self.context)
        worktree_path = self._resolved_worktree_path()

        log_json(
            "INFO",
            "Closing task git environment",
            task_id=self.context.task_id,
            branch=branch_name,
            worktree_path=str(worktree_path),
        )

        worktree_removed, branch_deleted = self._cleanup_worktree_branch(
            repository_root,
            branch_name,
            worktree_path,
            missing_worktree_is_removed=True,
        )

        return {
            "action": "close",
            "task_id": self.context.task_id,
            "branch": branch_name,
            "base_branch": self.context.base_branch,
            "worktree_path": str(worktree_path),
            "worktree_removed": worktree_removed,
            "branch_deleted": branch_deleted,
        }
