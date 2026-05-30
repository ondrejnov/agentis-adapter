import shutil
import subprocess
from pathlib import Path

from common.models import AgentExecutionContextPayload
from common.integrations.github_pr import GithubPrService


def _make_context() -> AgentExecutionContextPayload:
    return AgentExecutionContextPayload(
        run_id="run-1",
        task_id="task-1",
        title="Implement feature",
        description="Add the missing feature.",
        project_slug="agentis",
        project_github_repo="example/repo",
        base_branch="main",
        working_dir="/var/www/repo",
    )


def test_ensure_pull_request_skips_creation_without_commits(monkeypatch, tmp_path: Path):
    commands: list[list[str]] = []

    def fake_run(
        args: list[str],
        cwd: str,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(args)

        if args[:4] == ["gh", "pr", "view", "task-1"]:
            return subprocess.CompletedProcess(args, 1, "", "no pull requests found")
        if args[:4] == ["git", "rev-parse", "--verify", "--quiet"]:
            return subprocess.CompletedProcess(args, 0, "origin/main\n", "")
        if args[:3] == ["git", "rev-list", "--count"]:
            return subprocess.CompletedProcess(args, 0, "0\n", "")

        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    service = GithubPrService(
        context=_make_context(),
        worktree_path=tmp_path,
        branch="task-1",
    )

    assert service.ensure_pull_request() is None
    assert commands == [
        ["gh", "pr", "view", "task-1", "--json", "url,state"],
        ["git", "rev-parse", "--verify", "--quiet", "origin/main^{commit}"],
        ["git", "rev-list", "--count", "origin/main..task-1"],
    ]


def test_ensure_pull_request_pushes_branch_before_creation(monkeypatch, tmp_path: Path):
    commands: list[list[str]] = []

    def fake_run(
        args: list[str],
        cwd: str,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(args)

        if args[:4] == ["gh", "pr", "view", "task-1"]:
            return subprocess.CompletedProcess(args, 1, "", "no pull requests found")
        if args[:4] == ["git", "rev-parse", "--verify", "--quiet"]:
            return subprocess.CompletedProcess(args, 0, "origin/main\n", "")
        if args[:3] == ["git", "rev-list", "--count"]:
            return subprocess.CompletedProcess(args, 0, "2\n", "")
        if args[:4] == ["git", "config", "--get", "branch.main.remote"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        if args == ["git", "remote"]:
            return subprocess.CompletedProcess(args, 0, "origin\n", "")
        if args[:4] == ["git", "push", "--set-upstream", "origin"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(args, 0, "https://github.com/example/repo/pull/42\n", "")

        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    service = GithubPrService(
        context=_make_context(),
        worktree_path=tmp_path,
        branch="task-1",
    )

    assert service.ensure_pull_request() == "https://github.com/example/repo/pull/42"
    assert commands == [
        ["gh", "pr", "view", "task-1", "--json", "url,state"],
        ["git", "rev-parse", "--verify", "--quiet", "origin/main^{commit}"],
        ["git", "rev-list", "--count", "origin/main..task-1"],
        ["git", "config", "--get", "branch.main.remote"],
        ["git", "remote"],
        ["git", "push", "--set-upstream", "origin", "task-1:refs/heads/task-1"],
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            "task-1",
            "--title",
            "Implement feature",
            "--body",
            "Add the missing feature.",
            "--repo",
            "example/repo",
        ],
    ]


def test_ensure_pull_request_reuses_open_pull_request(monkeypatch, tmp_path: Path):
    commands: list[list[str]] = []

    def fake_run(
        args: list[str],
        cwd: str,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(args)

        if args[:4] == ["gh", "pr", "view", "task-1"]:
            return subprocess.CompletedProcess(
                args,
                0,
                '{"url":"https://github.com/example/repo/pull/41","state":"OPEN"}\n',
                "",
            )
        if args[:4] == ["git", "config", "--get", "branch.main.remote"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        if args == ["git", "remote"]:
            return subprocess.CompletedProcess(args, 0, "origin\n", "")
        if args[:4] == ["git", "push", "--set-upstream", "origin"]:
            return subprocess.CompletedProcess(args, 0, "", "")

        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    service = GithubPrService(
        context=_make_context(),
        worktree_path=tmp_path,
        branch="task-1",
    )

    result = service.ensure_pull_request_result()

    assert result is not None
    assert result.url == "https://github.com/example/repo/pull/41"
    assert result.created is False
    assert commands == [
        ["gh", "pr", "view", "task-1", "--json", "url,state"],
        ["git", "config", "--get", "branch.main.remote"],
        ["git", "remote"],
        ["git", "push", "--set-upstream", "origin", "task-1:refs/heads/task-1"],
    ]


def test_ensure_pull_request_creates_new_pull_request_when_existing_is_closed(monkeypatch, tmp_path: Path):
    commands: list[list[str]] = []

    def fake_run(
        args: list[str],
        cwd: str,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(args)

        if args[:4] == ["gh", "pr", "view", "task-1"]:
            return subprocess.CompletedProcess(
                args,
                0,
                '{"url":"https://github.com/example/repo/pull/40","state":"CLOSED"}\n',
                "",
            )
        if args[:4] == ["git", "rev-parse", "--verify", "--quiet"]:
            return subprocess.CompletedProcess(args, 0, "origin/main\n", "")
        if args[:3] == ["git", "rev-list", "--count"]:
            return subprocess.CompletedProcess(args, 0, "2\n", "")
        if args[:4] == ["git", "config", "--get", "branch.main.remote"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        if args == ["git", "remote"]:
            return subprocess.CompletedProcess(args, 0, "origin\n", "")
        if args[:4] == ["git", "push", "--set-upstream", "origin"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(args, 0, "https://github.com/example/repo/pull/42\n", "")

        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    service = GithubPrService(
        context=_make_context(),
        worktree_path=tmp_path,
        branch="task-1",
    )

    result = service.ensure_pull_request_result()

    assert result is not None
    assert result.url == "https://github.com/example/repo/pull/42"
    assert result.created is True
    assert commands == [
        ["gh", "pr", "view", "task-1", "--json", "url,state"],
        ["git", "rev-parse", "--verify", "--quiet", "origin/main^{commit}"],
        ["git", "rev-list", "--count", "origin/main..task-1"],
        ["git", "config", "--get", "branch.main.remote"],
        ["git", "remote"],
        ["git", "push", "--set-upstream", "origin", "task-1:refs/heads/task-1"],
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            "task-1",
            "--title",
            "Implement feature",
            "--body",
            "Add the missing feature.",
            "--repo",
            "example/repo",
        ],
    ]
