"""Git/worktree adapter layer.

``GitAdapterService`` sits between the minimal :class:`BaseAdapterService` (which
only knows how to talk to Agentis and run an agent) and the concrete adapters. It
owns everything git: resolving the repository, creating/reusing a per-task
worktree and naming branches. Followup actions (merging the task branch, tearing
the worktree/branch down) are not adapter code anymore — they live in named
workflows (`.agentis/workflows/<name>.yaml`) selected via
``context.adapter.workflow``.

Concrete adapters (the local CLI adapters) subclass this and add their own
session lifecycle on top.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from common.models import AgentExecutionContextPayload
from common.adapter_base import BaseAdapterService, log_json
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


__all__ = ["GitAdapterService"]
