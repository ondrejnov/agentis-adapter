"""Thin wrapper around the `gh` CLI for managing task pull requests."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from common.models import AgentExecutionContextPayload


class GithubPrError(RuntimeError):
    """Raised when the `gh` CLI cannot view or create a pull request."""


@dataclass(frozen=True)
class GithubPrResult:
    url: str
    created: bool


class GithubPrService:
    def __init__(
        self,
        context: AgentExecutionContextPayload,
        worktree_path: Path,
        branch: str,
        gh_binary: str = "gh",
    ) -> None:
        self.context = context
        self.worktree_path = worktree_path
        self.branch = branch
        self.gh_binary = gh_binary

    def ensure_pull_request_result(self) -> GithubPrResult | None:
        if shutil.which(self.gh_binary) is None:
            raise GithubPrError(f"{self.gh_binary} CLI is not available on PATH")
        if not self.worktree_path.is_dir():
            raise GithubPrError(f"Worktree path {self.worktree_path} does not exist")

        existing = self._view_pr()
        if existing:
            self._push_branch()
            return GithubPrResult(url=existing, created=False)

        if not self._has_commits_between_base_and_head():
            return None

        self._push_branch()
        return GithubPrResult(url=self._create_pr(), created=True)

    def ensure_pull_request(self) -> str | None:
        result = self.ensure_pull_request_result()
        return result.url if result else None

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.gh_binary, *args],
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
            check=False,
        )

    def _resolve_base_ref(self) -> str:
        candidates = [self.context.base_branch]
        if "/" not in self.context.base_branch:
            candidates.insert(0, f"origin/{self.context.base_branch}")

        for candidate in candidates:
            completed = self._run_git("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
            if completed.returncode == 0:
                return candidate

        raise GithubPrError(f"Base branch {self.context.base_branch!r} was not found")

    def _has_commits_between_base_and_head(self) -> bool:
        base_ref = self._resolve_base_ref()
        completed = self._run_git("rev-list", "--count", f"{base_ref}..{self.branch}")
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
            raise GithubPrError(f"Failed to compare {base_ref}..{self.branch}: {stderr}")

        return completed.stdout.strip() != "0"

    def _resolve_push_remote(self) -> str:
        completed = self._run_git("config", "--get", f"branch.{self.context.base_branch}.remote")
        if completed.returncode == 0:
            remote = completed.stdout.strip()
            if remote:
                return remote

        completed = self._run_git("remote")
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
            raise GithubPrError(f"Failed to list git remotes: {stderr}")

        remotes = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if "origin" in remotes:
            return "origin"
        if len(remotes) == 1:
            return remotes[0]

        raise GithubPrError("Could not determine git remote for branch push")

    def _push_branch(self) -> None:
        remote = self._resolve_push_remote()
        completed = self._run_git(
            "push",
            "--set-upstream",
            remote,
            f"{self.branch}:refs/heads/{self.branch}",
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
            raise GithubPrError(f"Failed to push branch {self.branch!r} to {remote!r}: {stderr}")

    def _view_pr(self) -> str | None:
        completed = self._run("pr", "view", self.branch, "--json", "url,state")
        if completed.returncode != 0:
            return None

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise GithubPrError(f"gh pr view returned invalid JSON: {completed.stdout!r}") from exc

        if not isinstance(payload, dict):
            raise GithubPrError(f"gh pr view returned unexpected payload: {payload!r}")

        if payload.get("state") != "OPEN":
            return None

        url = payload.get("url")
        return url.strip() if isinstance(url, str) and url.strip() else None

    def _create_pr(self) -> str:
        title = self.context.title.strip() or self.context.task_id
        body = self.context.description.strip() or title
        args = [
            "pr",
            "create",
            "--base",
            self.context.base_branch,
            "--head",
            self.branch,
            "--title",
            title,
            "--body",
            body,
        ]
        if self.context.project_github_repo:
            args.extend(["--repo", self.context.project_github_repo])

        completed = self._run(*args)
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown gh error"
            raise GithubPrError(f"gh pr create failed: {stderr}")

        for line in reversed(completed.stdout.strip().splitlines()):
            stripped = line.strip()
            if stripped.startswith("http://") or stripped.startswith("https://"):
                return stripped
        raise GithubPrError(f"gh pr create did not return a URL: {completed.stdout!r}")
