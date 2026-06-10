"""Git/worktree adapter layer.

``GitAdapterService`` sits between the minimal :class:`BaseAdapterService` (which
only knows how to talk to Agentis and run an agent) and the concrete adapters. It
owns everything git: resolving the repository, creating/reusing a per-task
worktree, naming branches, merging the task branch back into the base branch and
tearing the worktree down again.

Concrete adapters (the local CLI adapters) subclass this and add their own
session lifecycle on top.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from common.models import AgentExecutionContextPayload
from common.adapter_base import BaseAdapterService, log_json
from common.agentis import AgentisRunLogger
from common.artifacts.source_snapshot import restore_source_snapshot


class GitAdapterService(BaseAdapterService):
    """Adapter base that manages the git worktree/branch lifecycle for a task."""

    DEFAULT_WORKTREE_DIRNAME = "worktree"

    # ------------------------------------------------------------------
    # git plumbing
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Repository / worktree resolution
    # ------------------------------------------------------------------

    def _repository_root(self) -> Path:
        if not self.context.working_dir:
            raise RuntimeError("working_dir is required")
        repository_root = self._run_git(Path(self.context.working_dir), "rev-parse", "--show-toplevel")
        return Path(repository_root)

    def _project_workspace_path(self) -> Path:
        if not self.context.working_dir:
            raise RuntimeError("working_dir is required")
        try:
            return self._repository_root()
        except RuntimeError:
            return Path(self.context.working_dir)

    def _current_branch_or_none(self, workspace_path: Path) -> str | None:
        try:
            return self._run_git(workspace_path, "branch", "--show-current") or None
        except RuntimeError:
            return None

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
            return self._project_workspace_path()
        return self._resolved_worktree_path()

    # ------------------------------------------------------------------
    # Worktree lifecycle
    # ------------------------------------------------------------------

    def create_worktree(self) -> dict[str, str | None]:
        if self.is_project_scope(self.context):
            workspace_path = self._project_workspace_path()
            current_branch = self._current_branch_or_none(workspace_path)
            return {
                "action": "create_worktree",
                "task_id": self.context.task_id,
                "branch": current_branch,
                "base_branch": self.context.base_branch,
                "working_dir": str(workspace_path),
                "status": "skipped",
                "reason": "project_scope",
            }

        repository_root = self._repository_root()
        print(f"Repository root: {repository_root}")

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

    def restore_snapshot(self, snapshot_key: str) -> dict[str, str | None]:
        worktree_path = self._workspace_path()
        result = restore_source_snapshot(worktree_path, snapshot_key)
        if result.status != "success":
            raise RuntimeError(result.reason or f"source snapshot restore {result.status}")
        log_json(
            "INFO",
            "Source snapshot restored",
            task_id=self.context.task_id,
            run_id=self.context.run_id,
            snapshot_key=snapshot_key,
            working_dir=str(worktree_path),
        )
        return {
            "action": "undo",
            "task_id": self.context.task_id,
            "run_id": self.context.run_id,
            "snapshot_key": snapshot_key,
            "working_dir": str(worktree_path),
        }

    def git_merge(self, message: str | None = None) -> dict[str, str | None]:
        """Rebase the task branch and fast-forward it into the project's base branch."""
        del message
        if self.is_project_scope(self.context):
            workspace_path = self._project_workspace_path()
            current_branch = self._current_branch_or_none(workspace_path)
            return {
                "action": "git_merge",
                "task_id": self.context.task_id,
                "branch": current_branch,
                "base_branch": self.context.base_branch,
                "status": "skipped",
                "reason": "project_scope",
                "repository_root": str(workspace_path),
            }

        repository_root = self._repository_root()

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

    def close(self) -> dict[str, str | bool | None]:
        if self.is_project_scope(self.context):
            workspace_path = self._project_workspace_path()
            current_branch = self._current_branch_or_none(workspace_path)
            return {
                "action": "close",
                "task_id": self.context.task_id,
                "branch": current_branch,
                "base_branch": self.context.base_branch,
                "status": "skipped",
                "reason": "project_scope",
                "repository_root": str(workspace_path),
                "worktree_removed": False,
                "branch_deleted": False,
            }

        repository_root = self._repository_root()

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


__all__ = ["GitAdapterService"]
