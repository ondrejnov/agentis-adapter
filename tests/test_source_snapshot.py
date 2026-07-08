from __future__ import annotations

from pathlib import Path

from common.artifacts import source_snapshot, work_snapshot


def _changes_diff_path(worktree: Path) -> Path:
    path = worktree / source_snapshot.CHANGES_DIFF_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_snapshot_sources_uses_native_snapshot_and_removes_previous_changes_diff(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (worktree / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (worktree / "ignored.log").write_text("ignored\n", encoding="utf-8")
    (worktree / ".git").mkdir()
    (worktree / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    old_diff = _changes_diff_path(worktree)
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
    diff_path = _changes_diff_path(worktree)
    diff_path.write_text("previous diff must be ignored\n", encoding="utf-8")

    result = source_snapshot.write_changes_diff(worktree, "snap-1")

    assert result.status == "success"
    assert diff_path == worktree / ".agentis" / ".local.changes"
    diff = diff_path.read_text(encoding="utf-8")
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
    changes_diff = _changes_diff_path(worktree)
    changes_diff.write_text("diff", encoding="utf-8")

    result = source_snapshot.restore_source_snapshot(worktree, "snap-1")

    assert result.status == "success"
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == "before\n"
    assert not (worktree / "extra.txt").exists()
    assert (worktree / "ignored.log").read_text(encoding="utf-8") == "ignored\n"
    assert not changes_diff.exists()


def test_restore_metadata_falls_back_when_utime_follow_symlinks_is_unavailable(monkeypatch, tmp_path: Path):
    path = tmp_path / "tracked.txt"
    path.write_text("tracked\n", encoding="utf-8")
    entry: dict[str, object] = {"mode": path.stat().st_mode, "mtime_ns": path.stat().st_mtime_ns}
    calls: list[bool | None] = []
    original_utime = work_snapshot.os.utime

    def fake_utime(path_arg, *args, **kwargs):
        calls.append(kwargs.get("follow_symlinks"))
        if kwargs.get("follow_symlinks") is False:
            raise NotImplementedError("follow_symlinks unavailable on this platform")
        return original_utime(path_arg, *args, **kwargs)

    monkeypatch.setattr(work_snapshot.os, "utime", fake_utime)

    work_snapshot._restore_metadata(path, entry, follow_symlinks=False)

    assert calls == [False, None]


def test_copy2_delegates_to_shutil_copy2_without_following_symlinks(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("source\n", encoding="utf-8")
    copy_calls: list[tuple[str, str, bool]] = []

    def fake_copy2(source_arg: str, target_arg: str, *, follow_symlinks: bool) -> None:
        copy_calls.append((source_arg, target_arg, follow_symlinks))
        Path(target_arg).write_bytes(Path(source_arg).read_bytes())

    monkeypatch.setattr(work_snapshot.shutil, "copy2", fake_copy2)

    work_snapshot._copy2(source, target)

    assert copy_calls == [(str(source), str(target), False)]
    assert target.read_text(encoding="utf-8") == "source\n"


def test_snapshot_sources_skips_missing_worktree(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")

    result = source_snapshot.snapshot_sources(tmp_path / "missing", "snap-1")
    assert result.status == "skipped"
    assert result.reason == "missing_worktree"


def test_source_snapshots_can_be_disabled_by_project_settings(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    settings_dir = worktree / ".agentis"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text('{"snapshots": false}\n', encoding="utf-8")
    (worktree / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    old_diff = _changes_diff_path(worktree)
    old_diff.write_text("previous diff", encoding="utf-8")
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")

    snapshot_result = source_snapshot.snapshot_sources(worktree, "snap-1")
    diff_result = source_snapshot.write_changes_diff(worktree, "snap-1")
    restore_result = source_snapshot.restore_source_snapshot(worktree, "snap-1")

    assert snapshot_result.status == "skipped"
    assert snapshot_result.reason == "disabled_by_project_settings"
    assert diff_result.status == "skipped"
    assert diff_result.reason == "disabled_by_project_settings"
    assert restore_result.status == "skipped"
    assert restore_result.reason == "disabled_by_project_settings"
    assert not old_diff.exists()
    assert not (tmp_path / "snapshots").exists()


def test_build_snapshot_key_sanitizes_parts():
    assert source_snapshot.build_snapshot_key(" task/42 ", "run:7") == "task-42-run-7"
    assert source_snapshot.build_snapshot_key(None, "...") == "snapshot"


def test_snapshot_store_dir_hashes_long_keys(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    long_key = "workflow-" + "a" * 120

    key_dir = source_snapshot._snapshot_key_dir(long_key)

    assert key_dir != source_snapshot.build_snapshot_key(long_key)
    assert key_dir.startswith("workflow-")
    assert len(key_dir) <= source_snapshot._MAX_SNAPSHOT_KEY_DIR_LENGTH
    assert source_snapshot._snapshot_store_dir(long_key) == tmp_path / "snapshots" / key_dir / "source-store"
    assert source_snapshot._snapshot_current_store_dir(long_key) == tmp_path / "snapshots" / key_dir / "current-store"
    assert source_snapshot._snapshot_key_dir("snap-1") == "snap-1"
