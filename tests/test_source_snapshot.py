from __future__ import annotations

from pathlib import Path

from common.artifacts import source_snapshot


def test_snapshot_sources_uses_native_snapshot_and_removes_previous_changes_diff(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (worktree / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (worktree / "ignored.log").write_text("ignored\n", encoding="utf-8")
    (worktree / ".git").mkdir()
    (worktree / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    old_diff = worktree / source_snapshot.CHANGES_DIFF_NAME
    old_diff.write_text("previous diff", encoding="utf-8")

    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")

    result = source_snapshot.snapshot_sources(worktree, "snap-1")

    assert result.status == "success"
    assert not old_diff.exists()
    files_dir = Path(result.snapshot_dir) / "files"
    assert (files_dir / ".gitignore").is_file()
    assert (files_dir / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"
    assert not (files_dir / "ignored.log").exists()
    assert not (files_dir / ".git").exists()


def test_snapshot_sources_replaces_previous_snapshot_for_same_key(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    tracked = worktree / "tracked.txt"
    tracked.write_text("first\n", encoding="utf-8")

    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")

    first = source_snapshot.snapshot_sources(worktree, "snap-1")
    tracked.write_text("second\n", encoding="utf-8")
    second = source_snapshot.snapshot_sources(worktree, "snap-1")

    assert first.status == "success"
    assert second.status == "success"
    assert (Path(second.snapshot_dir) / "files" / "tracked.txt").read_text(encoding="utf-8") == "second\n"


def test_write_changes_diff_records_modified_and_created_files(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (worktree / "changed.txt").write_text("old\n", encoding="utf-8")

    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")

    snapshot_result = source_snapshot.snapshot_sources(worktree, "snap-1")
    assert snapshot_result.status == "success"

    (worktree / "changed.txt").write_text("new\n", encoding="utf-8")
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
    assert str(tmp_path / "snapshots") not in diff
    assert f"--- {snapshot_result.snapshot_dir}/files/changed.txt" not in diff
    assert "--- changed.txt" in diff
    assert "+++ changed.txt" in diff
    assert "+++ created.txt" in diff


def test_write_changes_diff_skips_when_snapshot_is_missing(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")

    result = source_snapshot.write_changes_diff(worktree, "snap-1")

    assert result.status == "skipped"
    assert result.reason == "missing_snapshot"


def test_restore_source_snapshot_restores_snapshot_and_preserves_ignored(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (worktree / "tracked.txt").write_text("before\n", encoding="utf-8")

    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")

    snapshot_result = source_snapshot.snapshot_sources(worktree, "snap-1")
    assert snapshot_result.status == "success"

    (worktree / "tracked.txt").write_text("after\n", encoding="utf-8")
    (worktree / "extra.txt").write_text("extra\n", encoding="utf-8")
    (worktree / "ignored.log").write_text("ignored\n", encoding="utf-8")
    changes_diff = worktree / source_snapshot.CHANGES_DIFF_NAME
    changes_diff.write_text("diff", encoding="utf-8")

    result = source_snapshot.restore_source_snapshot(worktree, "snap-1")

    assert result.status == "success"
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == "before\n"
    assert not (worktree / "extra.txt").exists()
    assert (worktree / "ignored.log").read_text(encoding="utf-8") == "ignored\n"
    assert not changes_diff.exists()


def test_snapshot_sources_skips_missing_worktree(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")

    result = source_snapshot.snapshot_sources(tmp_path / "missing", "snap-1")
    assert result.status == "skipped"
    assert result.reason == "missing_worktree"


def test_build_snapshot_key_sanitizes_parts():
    assert source_snapshot.build_snapshot_key(" task/42 ", "run:7") == "task-42-run-7"
    assert source_snapshot.build_snapshot_key(None, "...") == "snapshot"
