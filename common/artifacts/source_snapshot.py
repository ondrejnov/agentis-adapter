from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.artifacts import work_snapshot

SNAPSHOT_ROOT = Path(tempfile.gettempdir()) / "agentis-source-snapshots"
CHANGES_DIFF_NAME = ".changes.diff"
_DIFF_EXCLUDES = (CHANGES_DIFF_NAME, "__pycache__", ".pytest_cache", ".ruff_cache")


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
    store_dir = _snapshot_store_dir(snapshot_key)
    result_base = {
        "key": snapshot_key,
        "worktree": str(worktree_path),
        "snapshot_dir": str(store_dir),
    }
    if not worktree_path.is_dir():
        return SourceSnapshotResult(status="skipped", reason="missing_worktree", **result_base)

    _remove_existing_changes_diff(worktree_path)
    shutil.rmtree(store_dir, ignore_errors=True)
    try:
        snapshot_dir = work_snapshot.create_snapshot(worktree_path, store=store_dir, name="source")
    except Exception as exc:  # noqa: BLE001
        return SourceSnapshotResult(status="failed", reason=str(exc), **result_base)

    return SourceSnapshotResult(status="success", snapshot_dir=str(snapshot_dir), **_without_snapshot_dir(result_base))


def write_changes_diff(worktree: str | Path, snapshot_key: str) -> SourceSnapshotResult:
    worktree_path = Path(worktree)
    store_dir = _snapshot_store_dir(snapshot_key)
    current_store_dir = _snapshot_current_store_dir(snapshot_key)
    diff_path = worktree_path / CHANGES_DIFF_NAME
    result_base = {
        "key": snapshot_key,
        "worktree": str(worktree_path),
        "snapshot_dir": str(store_dir),
        "diff_path": str(diff_path),
    }
    if not worktree_path.is_dir():
        return SourceSnapshotResult(status="skipped", reason="missing_worktree", **result_base)

    try:
        snapshot_dir = _latest_snapshot_dir(worktree_path, store_dir)
    except Exception as exc:  # noqa: BLE001
        return SourceSnapshotResult(status="failed", reason=str(exc), **result_base)
    if snapshot_dir is None:
        return SourceSnapshotResult(status="skipped", reason="missing_snapshot", **result_base)

    _remove_existing_changes_diff(worktree_path)
    shutil.rmtree(current_store_dir, ignore_errors=True)
    try:
        current_dir = work_snapshot.create_snapshot(worktree_path, store=current_store_dir, name="current")
    except Exception as exc:  # noqa: BLE001
        return SourceSnapshotResult(status="failed", reason=str(exc), **result_base)

    args = ["diff", "-ruN"]
    for pattern in _DIFF_EXCLUDES:
        args.extend(["-x", pattern])
    args.extend([str(snapshot_dir / "files"), str(current_dir / "files")])

    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    if completed.returncode not in (0, 1):
        reason = (completed.stderr or completed.stdout or "diff failed").strip()
        return SourceSnapshotResult(status="failed", reason=reason, **result_base)

    diff_content = _normalize_changes_diff_paths(completed.stdout, snapshot_dir / "files", current_dir / "files")
    diff_path.write_text(diff_content, encoding="utf-8")
    return SourceSnapshotResult(status="success", snapshot_dir=str(snapshot_dir), **_without_snapshot_dir(result_base))


def restore_source_snapshot(worktree: str | Path, snapshot_key: str) -> SourceSnapshotResult:
    worktree_path = Path(worktree)
    store_dir = _snapshot_store_dir(snapshot_key)
    result_base = {
        "key": snapshot_key,
        "worktree": str(worktree_path),
        "snapshot_dir": str(store_dir),
    }
    if not worktree_path.is_dir():
        return SourceSnapshotResult(status="skipped", reason="missing_worktree", **result_base)

    try:
        snapshot_dir = _latest_snapshot_dir(worktree_path, store_dir)
    except Exception as exc:  # noqa: BLE001
        return SourceSnapshotResult(status="failed", reason=str(exc), **result_base)
    if snapshot_dir is None:
        return SourceSnapshotResult(status="skipped", reason="missing_snapshot", **result_base)

    try:
        work_snapshot.restore_snapshot(snapshot_dir, worktree_path, store=store_dir, delete=True)
    except Exception as exc:  # noqa: BLE001
        return SourceSnapshotResult(status="failed", reason=str(exc), **result_base)

    _remove_existing_changes_diff(worktree_path)
    return SourceSnapshotResult(status="success", snapshot_dir=str(snapshot_dir), **_without_snapshot_dir(result_base))


def snapshot_sources_best_effort(worktree: str | Path, snapshot_key: str, *, label: str) -> SourceSnapshotResult:
    try:
        result = snapshot_sources(worktree, snapshot_key)
    except Exception as exc:  # noqa: BLE001
        result = SourceSnapshotResult(
            status="failed",
            key=snapshot_key,
            worktree=str(worktree),
            snapshot_dir=str(_snapshot_store_dir(snapshot_key)),
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
            snapshot_dir=str(_snapshot_store_dir(snapshot_key)),
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
            snapshot_dir=str(_snapshot_store_dir(snapshot_key)),
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
    return None


def _snapshot_store_dir(snapshot_key: str) -> Path:
    return SNAPSHOT_ROOT / build_snapshot_key(snapshot_key) / "source-store"


def _snapshot_current_store_dir(snapshot_key: str) -> Path:
    return SNAPSHOT_ROOT / build_snapshot_key(snapshot_key) / "current-store"


def _latest_snapshot_dir(worktree_path: Path, store_dir: Path) -> Path | None:
    snapshots = work_snapshot.list_snapshots(worktree_path, store=store_dir)
    if not snapshots:
        return None
    path = snapshots[0].get("path")
    if not isinstance(path, str):
        raise RuntimeError("Snapshot manifest does not contain a path")
    return Path(path)


def _without_snapshot_dir(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key != "snapshot_dir"}


def _normalize_changes_diff_paths(diff_content: str, source_files_dir: Path, current_files_dir: Path) -> str:
    bases = (source_files_dir, current_files_dir)
    return "".join(_normalize_changes_diff_line(line, bases) for line in diff_content.splitlines(keepends=True))


def _normalize_changes_diff_line(line: str, bases: tuple[Path, Path]) -> str:
    if line.startswith(("--- ", "+++ ")):
        return _normalize_diff_file_header(line, bases)

    if line.startswith(("diff ", "Binary files ", "Files ", "Only in ")):
        return _replace_snapshot_path_prefixes(line, bases)

    return line


def _normalize_diff_file_header(line: str, bases: tuple[Path, Path]) -> str:
    marker = line[:4]
    rest = line[4:]
    path, separator, suffix = rest.partition("\t")
    relative_path = _relative_snapshot_path(path, bases)
    if relative_path is None:
        return line
    return f"{marker}{relative_path}{separator}{suffix}"


def _replace_snapshot_path_prefixes(line: str, bases: tuple[Path, Path]) -> str:
    normalized = line
    for base in bases:
        normalized = normalized.replace(f"{base}/", "")
        normalized = normalized.replace(str(base), ".")
    return normalized


def _relative_snapshot_path(path: str, bases: tuple[Path, Path]) -> str | None:
    if path == "/dev/null":
        return None

    candidate = Path(path)
    for base in bases:
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            continue
    return None


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
