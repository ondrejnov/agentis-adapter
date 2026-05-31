from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


SNAPSHOT_ROOT = Path("/tmp/agentis-source-snapshots")
CHANGES_DIFF_NAME = ".changes.diff"
_EXCLUDES = (".git/", CHANGES_DIFF_NAME, "__pycache__/", ".pytest_cache/", ".ruff_cache/")


@dataclass(frozen=True)
class SourceSnapshotResult:
    status: str
    key: str
    worktree: str
    snapshot_dir: str
    diff_path: str | None = None
    reason: str | None = None


def build_snapshot_key(*parts: str | None) -> str:
    raw = "-".join(part.strip() for part in parts if part and part.strip())
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")
    return sanitized or "snapshot"


def snapshot_sources(worktree: str | Path, snapshot_key: str) -> SourceSnapshotResult:
    worktree_path = Path(worktree)
    snapshot_dir = _snapshot_source_dir(snapshot_key)
    result_base = {
        "key": snapshot_key,
        "worktree": str(worktree_path),
        "snapshot_dir": str(snapshot_dir),
    }
    if not worktree_path.is_dir():
        return SourceSnapshotResult(status="skipped", reason="missing_worktree", **result_base)
    _remove_existing_changes_diff(worktree_path)
    if shutil.which("rsync") is None:
        return SourceSnapshotResult(status="skipped", reason="missing_rsync", **result_base)

    snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
    completed = _rsync_filtered(worktree_path, snapshot_dir)
    if completed.returncode != 0:
        reason = (completed.stderr or completed.stdout or "rsync failed").strip()
        return SourceSnapshotResult(status="failed", reason=reason, **result_base)
    return SourceSnapshotResult(status="success", **result_base)


def write_changes_diff(worktree: str | Path, snapshot_key: str) -> SourceSnapshotResult:
    worktree_path = Path(worktree)
    snapshot_dir = _snapshot_source_dir(snapshot_key)
    current_dir = _snapshot_current_dir(snapshot_key)
    diff_path = worktree_path / CHANGES_DIFF_NAME
    result_base = {
        "key": snapshot_key,
        "worktree": str(worktree_path),
        "snapshot_dir": str(snapshot_dir),
        "diff_path": str(diff_path),
    }
    if not worktree_path.is_dir():
        return SourceSnapshotResult(status="skipped", reason="missing_worktree", **result_base)
    if not snapshot_dir.is_dir():
        return SourceSnapshotResult(status="skipped", reason="missing_snapshot", **result_base)
    if shutil.which("rsync") is None:
        return SourceSnapshotResult(status="skipped", reason="missing_rsync", **result_base)

    current_dir.parent.mkdir(parents=True, exist_ok=True)
    rsync_completed = _rsync_filtered(worktree_path, current_dir)
    if rsync_completed.returncode != 0:
        reason = (rsync_completed.stderr or rsync_completed.stdout or "rsync failed").strip()
        return SourceSnapshotResult(status="failed", reason=reason, **result_base)

    args = ["diff", "-ruN"]
    for pattern in _EXCLUDES:
        args.extend(["-x", pattern.rstrip("/")])
    args.extend([str(snapshot_dir), str(current_dir)])

    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    if completed.returncode not in (0, 1):
        reason = (completed.stderr or completed.stdout or "diff failed").strip()
        return SourceSnapshotResult(status="failed", reason=reason, **result_base)

    diff_path.write_text(completed.stdout, encoding="utf-8")
    return SourceSnapshotResult(status="success", **result_base)


def restore_source_snapshot(worktree: str | Path, snapshot_key: str) -> SourceSnapshotResult:
    worktree_path = Path(worktree)
    snapshot_dir = _snapshot_source_dir(snapshot_key)
    result_base = {
        "key": snapshot_key,
        "worktree": str(worktree_path),
        "snapshot_dir": str(snapshot_dir),
    }
    if not worktree_path.is_dir():
        return SourceSnapshotResult(status="skipped", reason="missing_worktree", **result_base)
    if not snapshot_dir.is_dir():
        return SourceSnapshotResult(status="skipped", reason="missing_snapshot", **result_base)
    if shutil.which("rsync") is None:
        return SourceSnapshotResult(status="skipped", reason="missing_rsync", **result_base)

    completed = _rsync_restore_filtered(snapshot_dir, worktree_path)
    _remove_existing_changes_diff(worktree_path)
    if completed.returncode != 0:
        reason = (completed.stderr or completed.stdout or "rsync failed").strip()
        return SourceSnapshotResult(status="failed", reason=reason, **result_base)
    return SourceSnapshotResult(status="success", **result_base)


def snapshot_sources_best_effort(worktree: str | Path, snapshot_key: str, *, label: str) -> SourceSnapshotResult:
    try:
        result = snapshot_sources(worktree, snapshot_key)
    except Exception as exc:  # noqa: BLE001
        result = SourceSnapshotResult(
            status="failed",
            key=snapshot_key,
            worktree=str(worktree),
            snapshot_dir=str(_snapshot_source_dir(snapshot_key)),
            reason=str(exc),
        )
    _log_result(label, result)
    return result


def write_changes_diff_best_effort(worktree: str | Path, snapshot_key: str, *, label: str) -> SourceSnapshotResult:
    try:
        result = write_changes_diff(worktree, snapshot_key)
    except Exception as exc:  # noqa: BLE001
        result = SourceSnapshotResult(
            status="failed",
            key=snapshot_key,
            worktree=str(worktree),
            snapshot_dir=str(_snapshot_source_dir(snapshot_key)),
            diff_path=str(Path(worktree) / CHANGES_DIFF_NAME),
            reason=str(exc),
        )
    _log_result(label, result)
    return result


def restore_source_snapshot_best_effort(worktree: str | Path, snapshot_key: str, *, label: str) -> SourceSnapshotResult:
    try:
        result = restore_source_snapshot(worktree, snapshot_key)
    except Exception as exc:  # noqa: BLE001
        result = SourceSnapshotResult(
            status="failed",
            key=snapshot_key,
            worktree=str(worktree),
            snapshot_dir=str(_snapshot_source_dir(snapshot_key)),
            reason=str(exc),
        )
    _log_result(label, result)
    return result


def changes_diff_attachment(result: SourceSnapshotResult) -> dict[str, str] | None:
    if result.status != "success" or not result.diff_path:
        return None

    try:
        diff_content = Path(result.diff_path).read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"[source-snapshot] failed to read changes diff {result.diff_path}: {exc}\n")
        return None

    if len(diff_content) > 0:
        return {"label": "Changes diff", "value": diff_content, "type": "diff"}


def _snapshot_source_dir(snapshot_key: str) -> Path:
    return SNAPSHOT_ROOT / build_snapshot_key(snapshot_key) / "source"


def _snapshot_current_dir(snapshot_key: str) -> Path:
    return SNAPSHOT_ROOT / build_snapshot_key(snapshot_key) / "current"


def _rsync_filtered(source_dir: Path, target_dir: Path) -> subprocess.CompletedProcess[str]:
    args = ["rsync", "-a", "--delete", "--delete-excluded", "--filter", ":- .gitignore"]
    for pattern in _EXCLUDES:
        args.extend(["--exclude", pattern])
    args.extend([f"{source_dir}/", f"{target_dir}/"])
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _rsync_restore_filtered(source_dir: Path, target_dir: Path) -> subprocess.CompletedProcess[str]:
    args = ["rsync", "-a", "--delete", "--filter", ":- .gitignore"]
    for pattern in _EXCLUDES:
        args.extend(["--exclude", pattern])
    args.extend([f"{source_dir}/", f"{target_dir}/"])
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _remove_existing_changes_diff(worktree_path: Path) -> None:
    try:
        (worktree_path / CHANGES_DIFF_NAME).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        sys.stderr.write(f"[source-snapshot] failed to remove existing {CHANGES_DIFF_NAME}: {exc}\n")


def _log_result(label: str, result: SourceSnapshotResult) -> None:
    if result.status == "success":
        return
    sys.stderr.write(
        f"[source-snapshot] {label} {result.status}: key={result.key} "
        f"worktree={result.worktree} reason={result.reason}\n"
    )
