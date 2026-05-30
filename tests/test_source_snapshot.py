from __future__ import annotations

import subprocess
import shutil
from pathlib import Path
from typing import Any

from common.artifacts import source_snapshot


def test_snapshot_sources_uses_rsync_and_removes_previous_changes_diff(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    old_diff = worktree / source_snapshot.CHANGES_DIFF_NAME
    old_diff.write_text("previous diff", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    monkeypatch.setattr(source_snapshot.shutil, "which", lambda command: "/usr/bin/rsync" if command == "rsync" else None)

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(source_snapshot.subprocess, "run", fake_run)

    result = source_snapshot.snapshot_sources(worktree, "snap-1")

    assert result.status == "success"
    assert not old_diff.exists()
    assert calls == [
        [
            "rsync",
            "-a",
            "--delete",
            "--delete-excluded",
            "--filter",
            ":- .gitignore",
            "--exclude",
            ".git/",
            "--exclude",
            ".changes.diff",
            "--exclude",
            "__pycache__/",
            "--exclude",
            ".pytest_cache/",
            "--exclude",
            ".ruff_cache/",
            f"{worktree}/",
            f"{tmp_path / 'snapshots' / 'snap-1' / 'source'}/",
        ]
    ]


def test_write_changes_diff_records_modified_and_created_files(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    monkeypatch.setattr(source_snapshot.shutil, "which", lambda command: "/usr/bin/rsync" if command == "rsync" else None)
    snapshot = tmp_path / "snapshots" / "snap-1" / "source"
    worktree = tmp_path / "worktree"
    snapshot.mkdir(parents=True)
    worktree.mkdir()
    real_run = subprocess.run

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "rsync":
            source = Path(args[-2].rstrip("/"))
            target = Path(args[-1].rstrip("/"))
            ignored = _read_simple_gitignore(source)
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
            for path in source.rglob("*"):
                relative = path.relative_to(source)
                if relative.name == source_snapshot.CHANGES_DIFF_NAME or relative.as_posix() in ignored:
                    continue
                destination = target / relative
                if path.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(source_snapshot.subprocess, "run", fake_run)

    (snapshot / "changed.txt").write_text("old\n", encoding="utf-8")
    (snapshot / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (worktree / "changed.txt").write_text("new\n", encoding="utf-8")
    (worktree / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (worktree / "created.txt").write_text("created\n", encoding="utf-8")
    (worktree / "ignored.log").write_text("ignored\n", encoding="utf-8")
    (worktree / source_snapshot.CHANGES_DIFF_NAME).write_text("previous diff must be ignored\n", encoding="utf-8")

    result = source_snapshot.write_changes_diff(worktree, "snap-1")

    assert result.status == "success"
    diff = (worktree / source_snapshot.CHANGES_DIFF_NAME).read_text(encoding="utf-8")
    assert "-old" in diff
    assert "+new" in diff
    assert "created.txt" in diff
    assert "ignored.log" not in diff
    assert "previous diff must be ignored" not in diff


def _read_simple_gitignore(source: Path) -> set[str]:
    gitignore = source / ".gitignore"
    if not gitignore.is_file():
        return set()
    return {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines() if line.strip()}
