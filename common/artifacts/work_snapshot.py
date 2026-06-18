"""Create and restore native Python snapshots of a working tree.

The module intentionally has no third-party dependencies. It mirrors files into
an external snapshot directory, stores a JSON manifest, and skips files matched
by .gitignore files found while walking the tree.

Examples:
    python -m work_snapshot create .
    python -m work_snapshot create . --workers 8
    python -m work_snapshot list .
    python -m work_snapshot restore 20260617T120000Z-my-change .
    python -m work_snapshot restore /path/to/snapshot . --delete
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Sequence


MANIFEST_NAME = "manifest.json"
SNAPSHOT_FILES_DIR = "files"
SNAPSHOT_VERSION = 1


class SnapshotError(RuntimeError):
    """Raised when creating or restoring a snapshot cannot continue."""


def remove_tree(path: str | os.PathLike[str], *, ignore_errors: bool = False) -> None:
    """Remove a directory tree, using Windows extended-length paths when needed."""

    shutil.rmtree(_filesystem_path(path), ignore_errors=ignore_errors)


def _filesystem_path(path: str | os.PathLike[str]) -> str:
    path_string = os.fspath(path)
    if os.name != "nt" or path_string.startswith("\\\\?\\"):
        return path_string

    absolute = os.path.abspath(path_string)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute.lstrip("\\")
    return "\\\\?\\" + absolute


def _mkdir(path: Path, *, parents: bool = False, exist_ok: bool = False) -> None:
    if parents:
        os.makedirs(_filesystem_path(path), exist_ok=exist_ok)
        return

    try:
        os.mkdir(_filesystem_path(path))
    except FileExistsError:
        if not exist_ok or not _is_dir(path):
            raise


def _stat(path: Path, *, follow_symlinks: bool) -> os.stat_result:
    return os.stat(_filesystem_path(path), follow_symlinks=follow_symlinks)


def _copy2(source: Path, target: Path) -> None:
    shutil.copy2(_filesystem_path(source), _filesystem_path(target), follow_symlinks=False)


def _read_text(path: Path, *, encoding: str, errors: str | None = None) -> str:
    with open(_filesystem_path(path), encoding=encoding, errors=errors) as file:
        return file.read()


def _is_file(path: Path) -> bool:
    return os.path.isfile(_filesystem_path(path))


def _is_dir(path: Path) -> bool:
    return os.path.isdir(_filesystem_path(path))


def _is_symlink(path: Path) -> bool:
    return os.path.islink(_filesystem_path(path))


def _exists_or_symlink(path: Path) -> bool:
    return os.path.exists(_filesystem_path(path)) or _is_symlink(path)


@dataclass(frozen=True)
class IgnoreRule:
    """A single .gitignore rule, scoped to the directory that defined it."""

    base: str
    pattern: str
    negated: bool
    directory_only: bool
    anchored: bool
    has_slash: bool
    regex: re.Pattern[str]

    def matches(self, rel_path: str, is_dir: bool) -> bool:
        if self.directory_only and not is_dir:
            return False

        candidate = _relative_to_base(rel_path, self.base)
        if candidate is None or candidate == "":
            return False

        if self.anchored or self.has_slash:
            return self.regex.fullmatch(candidate) is not None

        name = candidate.rsplit("/", 1)[-1]
        return self.regex.fullmatch(name) is not None


def create_snapshot(
    root: str | os.PathLike[str] = ".",
    *,
    store: str | os.PathLike[str] | None = None,
    name: str | None = None,
    workers: int | None = None,
) -> Path:
    """Create a snapshot of *root* and return the snapshot directory.

    Files ignored by .gitignore are skipped. The .git directory and the snapshot
    store itself are always skipped to avoid copying repository internals or
    recursively copying snapshots.
    """

    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise SnapshotError(f"Root is not a directory: {root_path}")

    store_path = _resolve_store(store)
    worker_count = _resolve_workers(workers)
    project_dir = store_path / _project_key(root_path)
    snapshot_id = _snapshot_id(name)
    snapshot_dir = project_dir / snapshot_id
    temp_dir = project_dir / f".tmp-{snapshot_id}"

    if snapshot_dir.exists() or temp_dir.exists():
        raise SnapshotError(f"Snapshot already exists: {snapshot_dir}")

    files_dir = temp_dir / SNAPSHOT_FILES_DIR
    _mkdir(files_dir, parents=True)

    manifest: dict[str, object] = {
        "version": SNAPSHOT_VERSION,
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root_path),
        "entries": [],
    }
    entries: list[tuple[int, dict[str, object]]] = []
    excluded_paths = _snapshot_excluded_paths(root_path, store_path)
    max_pending = max(worker_count * 4, worker_count)

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            pending: dict[Future[dict[str, object]], int] = {}
            order = 0

            for entry in _walk_included(root_path, excluded_paths=excluded_paths):
                rel = entry.rel_path
                source = entry.path
                target = files_dir / rel
                stat = _stat(source, follow_symlinks=False)

                if entry.kind == "dir":
                    _mkdir(target, parents=True, exist_ok=True)
                    entries.append((order, _manifest_entry("dir", rel, stat)))
                    order += 1
                    continue

                _mkdir(target.parent, parents=True, exist_ok=True)

                if entry.kind == "symlink":
                    future = executor.submit(
                        _copy_symlink_to_snapshot,
                        source,
                        target,
                        rel,
                        stat,
                    )
                else:
                    future = executor.submit(
                        _copy_file_to_snapshot,
                        source,
                        target,
                        rel,
                        stat,
                    )
                pending[future] = order
                order += 1

                if len(pending) >= max_pending:
                    _collect_snapshot_results(pending, entries, wait_for_one=True)

            _collect_snapshot_results(pending, entries, wait_for_one=False)

        manifest["entries"] = [entry for _, entry in sorted(entries, key=lambda item: item[0])]
        _write_manifest(temp_dir / MANIFEST_NAME, manifest)
        os.replace(_filesystem_path(temp_dir), _filesystem_path(snapshot_dir))
    except Exception:
        remove_tree(temp_dir, ignore_errors=True)
        raise

    return snapshot_dir


def list_snapshots(
    root: str | os.PathLike[str] = ".",
    *,
    store: str | os.PathLike[str] | None = None,
) -> list[dict[str, object]]:
    """Return manifests for snapshots belonging to *root*, newest first."""

    root_path = Path(root).expanduser().resolve()
    project_dir = _resolve_store(store) / _project_key(root_path)
    if not project_dir.exists():
        return []

    snapshots: list[dict[str, object]] = []
    for manifest_path in project_dir.glob(f"*/{MANIFEST_NAME}"):
        try:
            manifest = _read_manifest(manifest_path.parent)
        except SnapshotError:
            continue
        manifest["path"] = str(manifest_path.parent)
        snapshots.append(manifest)

    return sorted(
        snapshots,
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    )


def restore_snapshot(
    snapshot: str | os.PathLike[str],
    target: str | os.PathLike[str] = ".",
    *,
    store: str | os.PathLike[str] | None = None,
    delete: bool = False,
    workers: int | None = None,
) -> Path:
    """Restore *snapshot* into *target* and return the target directory.

    By default this overwrites files present in the snapshot and leaves extra
    files in the target untouched. With delete=True it removes extra non-ignored
    files first, giving rsync --delete style behavior while preserving files
    ignored by the target's .gitignore.
    """

    target_path = Path(target).expanduser().resolve()
    _mkdir(target_path, parents=True, exist_ok=True)

    worker_count = _resolve_workers(workers)
    snapshot_dir = resolve_snapshot(snapshot, target_path, store=store)
    manifest = _read_manifest(snapshot_dir)
    entries = _manifest_entries(manifest)
    files_dir = snapshot_dir / SNAPSHOT_FILES_DIR
    if not _is_dir(files_dir):
        raise SnapshotError(f"Snapshot files directory is missing: {files_dir}")

    expected_paths = {str(entry["path"]) for entry in entries}
    expected_dirs = {
        parent.as_posix() for rel in expected_paths for parent in PurePosixPath(rel).parents if parent.as_posix() != "."
    }
    expected_paths.update(expected_dirs)

    excluded_paths = _snapshot_excluded_paths(target_path, _resolve_store(store))
    if delete:
        _delete_extra_paths(target_path, expected_paths, excluded_paths)

    dir_entries: list[dict[str, object]] = []
    max_pending = max(worker_count * 4, worker_count)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        pending: set[Future[None]] = set()

        for entry in entries:
            rel = str(entry["path"])
            kind = str(entry["type"])
            source = files_dir / rel
            destination = target_path / rel

            if kind == "dir":
                _mkdir(destination, parents=True, exist_ok=True)
                dir_entries.append(entry)
                continue

            _mkdir(destination.parent, parents=True, exist_ok=True)
            if _exists_or_symlink(destination):
                _remove_path(destination)

            if kind == "symlink":
                pending.add(
                    executor.submit(
                        _restore_symlink_from_snapshot,
                        str(entry["link_target"]),
                        destination,
                    )
                )
            elif kind == "file":
                pending.add(
                    executor.submit(
                        _restore_file_from_snapshot,
                        source,
                        destination,
                        entry,
                    )
                )
            else:
                raise SnapshotError(f"Unsupported manifest entry type: {kind}")

            if len(pending) >= max_pending:
                _wait_for_restore_results(pending, wait_for_one=True)

        _wait_for_restore_results(pending, wait_for_one=False)

    for entry in sorted(
        dir_entries,
        key=lambda item: str(item["path"]).count("/"),
        reverse=True,
    ):
        _restore_metadata(target_path / str(entry["path"]), entry, follow_symlinks=False)

    return target_path


def resolve_snapshot(
    snapshot: str | os.PathLike[str],
    root: str | os.PathLike[str] = ".",
    *,
    store: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve a snapshot id, prefix, or path for *root*."""

    snapshot_path = Path(snapshot).expanduser()
    if snapshot_path.exists():
        return snapshot_path.resolve()

    root_path = Path(root).expanduser().resolve()
    project_dir = _resolve_store(store) / _project_key(root_path)
    exact = project_dir / str(snapshot)
    if exact.exists():
        return exact.resolve()

    matches = sorted(project_dir.glob(f"{snapshot}*")) if project_dir.exists() else []
    matches = [match for match in matches if match.is_dir()]
    if len(matches) == 1:
        return matches[0].resolve()
    if len(matches) > 1:
        choices = ", ".join(match.name for match in matches)
        raise SnapshotError(f"Snapshot prefix is ambiguous: {snapshot} ({choices})")
    raise SnapshotError(f"Snapshot not found: {snapshot}")


@dataclass(frozen=True)
class WalkEntry:
    path: Path
    rel_path: str
    kind: str


def _copy_file_to_snapshot(
    source: Path,
    target: Path,
    rel_path: str,
    stat: os.stat_result,
) -> dict[str, object]:
    _copy2(source, target)
    return _manifest_entry("file", rel_path, stat)


def _copy_symlink_to_snapshot(
    source: Path,
    target: Path,
    rel_path: str,
    stat: os.stat_result,
) -> dict[str, object]:
    link_target = os.readlink(_filesystem_path(source))
    os.symlink(link_target, _filesystem_path(target))
    return _manifest_entry("symlink", rel_path, stat, link_target=link_target)


def _collect_snapshot_results(
    pending: dict[Future[dict[str, object]], int],
    entries: list[tuple[int, dict[str, object]]],
    *,
    wait_for_one: bool,
) -> None:
    if not pending:
        return

    if wait_for_one:
        done, _ = wait(pending, return_when=FIRST_COMPLETED)
    else:
        done = set(pending)

    for future in done:
        order = pending.pop(future)
        entries.append((order, future.result()))


def _restore_file_from_snapshot(
    source: Path,
    destination: Path,
    entry: dict[str, object],
) -> None:
    _copy2(source, destination)
    _restore_metadata(destination, entry, follow_symlinks=False)


def _restore_symlink_from_snapshot(link_target: str, destination: Path) -> None:
    os.symlink(link_target, _filesystem_path(destination))


def _wait_for_restore_results(
    pending: set[Future[None]],
    *,
    wait_for_one: bool,
) -> None:
    if not pending:
        return

    if wait_for_one:
        done, _ = wait(pending, return_when=FIRST_COMPLETED)
    else:
        done = set(pending)

    for future in done:
        pending.remove(future)
        future.result()


def _walk_included(
    root: Path,
    *,
    excluded_paths: Sequence[Path],
) -> Iterator[WalkEntry]:
    yield from _walk_directory(root, "", [], excluded_paths=excluded_paths)


def _walk_directory(
    directory: Path,
    rel_dir: str,
    inherited_rules: Sequence[IgnoreRule],
    *,
    excluded_paths: Sequence[Path],
) -> Iterator[WalkEntry]:
    rules = list(inherited_rules)
    rules.extend(_load_gitignore(directory / ".gitignore", rel_dir))

    with os.scandir(_filesystem_path(directory)) as scan:
        entries = sorted(scan, key=lambda item: item.name)

    for entry in entries:
        path = directory / entry.name
        rel_path = _join_rel(rel_dir, entry.name)

        if entry.name == ".git" or _is_excluded_path(path, excluded_paths):
            continue

        is_dir = entry.is_dir(follow_symlinks=False)
        if _is_ignored(rel_path, is_dir, rules):
            continue

        if entry.is_symlink():
            yield WalkEntry(path, rel_path, "symlink")
        elif is_dir:
            yield WalkEntry(path, rel_path, "dir")
            yield from _walk_directory(
                path,
                rel_path,
                rules,
                excluded_paths=excluded_paths,
            )
        elif entry.is_file(follow_symlinks=False):
            yield WalkEntry(path, rel_path, "file")


def _delete_extra_paths(
    target: Path,
    expected_paths: set[str],
    excluded_paths: Sequence[Path],
) -> None:
    _delete_extra_paths_in_directory(
        target,
        "",
        [],
        expected_paths,
        excluded_paths=excluded_paths,
    )


def _delete_extra_paths_in_directory(
    directory: Path,
    rel_dir: str,
    inherited_rules: Sequence[IgnoreRule],
    expected_paths: set[str],
    *,
    excluded_paths: Sequence[Path],
) -> None:
    rules = list(inherited_rules)
    rules.extend(_load_gitignore(directory / ".gitignore", rel_dir))

    with os.scandir(_filesystem_path(directory)) as scan:
        entries = sorted(scan, key=lambda item: item.name)

    for entry in entries:
        path = directory / entry.name
        rel_path = _join_rel(rel_dir, entry.name)

        if entry.name == ".git" or _is_excluded_path(path, excluded_paths):
            continue

        is_dir = entry.is_dir(follow_symlinks=False)
        if _is_ignored(rel_path, is_dir, rules):
            continue

        if entry.is_symlink() or entry.is_file(follow_symlinks=False):
            if rel_path not in expected_paths:
                _remove_path(path)
            continue

        if not is_dir:
            continue

        _delete_extra_paths_in_directory(
            path,
            rel_path,
            rules,
            expected_paths,
            excluded_paths=excluded_paths,
        )
        if rel_path not in expected_paths:
            try:
                os.rmdir(_filesystem_path(path))
            except OSError:
                pass


def _remove_path(path: Path) -> None:
    if _is_symlink(path) or _is_file(path):
        os.unlink(_filesystem_path(path))
    elif _is_dir(path):
        remove_tree(path)


def _load_gitignore(path: Path, base: str) -> list[IgnoreRule]:
    if not _is_file(path):
        return []

    rules: list[IgnoreRule] = []
    try:
        lines = _read_text(path, encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = _read_text(path, encoding="utf-8", errors="replace").splitlines()

    for raw_line in lines:
        rule = _parse_gitignore_line(raw_line, base)
        if rule is not None:
            rules.append(rule)
    return rules


def _parse_gitignore_line(line: str, base: str) -> IgnoreRule | None:
    line = _rstrip_unescaped_spaces(line)
    if not line or line.startswith("#"):
        return None

    negated = False
    if line.startswith("!"):
        negated = True
        line = line[1:]
    if not line:
        return None

    directory_only = line.endswith("/")
    while line.endswith("/"):
        line = line[:-1]
    if not line:
        return None

    anchored = line.startswith("/")
    if anchored:
        line = line.lstrip("/")
    if not line:
        return None

    has_slash = "/" in line
    regex = re.compile(_wildmatch_regex(line))
    return IgnoreRule(
        base=base,
        pattern=line,
        negated=negated,
        directory_only=directory_only,
        anchored=anchored,
        has_slash=has_slash,
        regex=regex,
    )


def _is_ignored(rel_path: str, is_dir: bool, rules: Sequence[IgnoreRule]) -> bool:
    ignored = False
    for rule in rules:
        if rule.matches(rel_path, is_dir):
            ignored = not rule.negated
    return ignored


def _wildmatch_regex(pattern: str) -> str:
    result: list[str] = ["^"]
    index = 0
    length = len(pattern)

    while index < length:
        char = pattern[index]

        if char == "*":
            if index + 1 < length and pattern[index + 1] == "*":
                after = index + 2
                if after < length and pattern[after] == "/":
                    result.append("(?:[^/]+/)*")
                    index += 3
                elif after == length:
                    result.append(".*")
                    index += 2
                else:
                    result.append(".*")
                    index += 2
                continue
            result.append("[^/]*")
        elif char == "?":
            result.append("[^/]")
        elif char == "[":
            translated, index = _translate_character_class(pattern, index)
            result.append(translated)
            continue
        elif char == "\\" and index + 1 < length:
            index += 1
            result.append(re.escape(pattern[index]))
        else:
            result.append(re.escape(char))
        index += 1

    result.append("$")
    return "".join(result)


def _translate_character_class(pattern: str, start: int) -> tuple[str, int]:
    index = start + 1
    length = len(pattern)
    if index < length and pattern[index] in "!^":
        index += 1
    if index < length and pattern[index] == "]":
        index += 1
    while index < length and pattern[index] != "]":
        index += 1
    if index >= length:
        return re.escape("["), start + 1

    content = pattern[start + 1 : index]
    content = content.replace("\\", "\\\\")
    if content.startswith("!"):
        content = "^" + content[1:]
    elif content.startswith("^"):
        content = "\\" + content
    return f"[{content}]", index + 1


def _rstrip_unescaped_spaces(line: str) -> str:
    end = len(line)
    while end > 0 and line[end - 1] == " ":
        backslashes = 0
        index = end - 2
        while index >= 0 and line[index] == "\\":
            backslashes += 1
            index -= 1
        if backslashes % 2 == 1:
            break
        end -= 1
    return line[:end]


def _relative_to_base(rel_path: str, base: str) -> str | None:
    if not base:
        return rel_path
    if rel_path == base:
        return ""
    prefix = f"{base}/"
    if rel_path.startswith(prefix):
        return rel_path[len(prefix) :]
    return None


def _join_rel(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _snapshot_excluded_paths(root: Path, store: Path) -> list[Path]:
    excluded: list[Path] = []
    try:
        store.relative_to(root)
    except ValueError:
        return excluded
    excluded.append(store)
    return excluded


def _is_excluded_path(path: Path, excluded_paths: Sequence[Path]) -> bool:
    resolved = Path(os.path.abspath(path))
    for excluded in excluded_paths:
        if resolved == excluded or _is_relative_to(resolved, excluded):
            return True
    return False


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _manifest_entry(
    kind: str,
    rel_path: str,
    stat: os.stat_result,
    *,
    link_target: str | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "type": kind,
        "path": rel_path,
        "mode": stat.st_mode,
        "mtime_ns": stat.st_mtime_ns,
    }
    if kind == "file":
        entry["size"] = stat.st_size
    if link_target is not None:
        entry["link_target"] = link_target
    return entry


def _restore_metadata(
    path: Path,
    entry: dict[str, object],
    *,
    follow_symlinks: bool,
) -> None:
    mode = entry["mode"]
    mtime_ns = entry["mtime_ns"]
    if not isinstance(mode, int) or not isinstance(mtime_ns, int):
        raise SnapshotError("Snapshot manifest metadata is invalid")
    filesystem_path = _filesystem_path(path)
    try:
        os.chmod(filesystem_path, mode, follow_symlinks=follow_symlinks)
    except (NotImplementedError, PermissionError):
        pass
    try:
        os.utime(filesystem_path, ns=(mtime_ns, mtime_ns), follow_symlinks=follow_symlinks)
    except NotImplementedError:
        if not follow_symlinks and _is_symlink(path):
            return
        os.utime(filesystem_path, ns=(mtime_ns, mtime_ns))


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    with open(_filesystem_path(path), "w", encoding="utf-8") as file:
        file.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _read_manifest(snapshot_dir: Path) -> dict[str, object]:
    manifest_path = snapshot_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SnapshotError(f"Snapshot manifest is missing: {manifest_path}")

    try:
        manifest = json.loads(_read_text(manifest_path, encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SnapshotError(f"Snapshot manifest is invalid: {manifest_path}") from error

    if manifest.get("version") != SNAPSHOT_VERSION:
        raise SnapshotError(f"Unsupported snapshot version in {manifest_path}")
    return manifest


def _manifest_entries(manifest: dict[str, object]) -> list[dict[str, object]]:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise SnapshotError("Snapshot manifest does not contain an entries list")
    return [entry for entry in entries if isinstance(entry, dict)]


def _resolve_workers(workers: int | None) -> int:
    if workers is None:
        return min(32, (os.cpu_count() or 1) + 1)
    if workers < 1:
        raise SnapshotError("Worker count must be at least 1")
    return workers


def _resolve_store(store: str | os.PathLike[str] | None) -> Path:
    if store is not None:
        return Path(store).expanduser().resolve()
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return (Path(state_home).expanduser() / "work-snapshots").resolve()
    return (Path.home() / ".local" / "state" / "work-snapshots").resolve()


def _project_key(root: Path) -> str:
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip(".-") or "project"
    return f"{slug}-{digest}"


def _snapshot_id(name: str | None) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not name:
        return timestamp
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return f"{timestamp}-{slug}" if slug else timestamp


def _format_snapshot(manifest: dict[str, object]) -> str:
    entries = _manifest_entries(manifest)
    snapshot_id = manifest.get("snapshot_id", "<unknown>")
    created_at = manifest.get("created_at", "<unknown>")
    path = manifest.get("path", "")
    return f"{snapshot_id}\t{created_at}\t{len(entries)} entries\t{path}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="work_snapshot",
        description="Create and restore native Python snapshots that respect .gitignore.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a snapshot")
    create.add_argument("root", nargs="?", default=".", help="directory to snapshot")
    create.add_argument("--store", help="snapshot store directory")
    create.add_argument("--name", help="optional suffix for the snapshot id")
    create.add_argument(
        "--workers",
        type=int,
        help="parallel copy worker count; default is min(32, os.cpu_count() + 1)",
    )

    list_command = subparsers.add_parser("list", help="list snapshots for a directory")
    list_command.add_argument("root", nargs="?", default=".", help="project directory")
    list_command.add_argument("--store", help="snapshot store directory")

    restore = subparsers.add_parser("restore", help="restore a snapshot")
    restore.add_argument("snapshot", help="snapshot id, unique prefix, or path")
    restore.add_argument("target", nargs="?", default=".", help="restore target directory")
    restore.add_argument("--store", help="snapshot store directory")
    restore.add_argument(
        "--workers",
        type=int,
        help="parallel copy worker count; default is min(32, os.cpu_count() + 1)",
    )
    restore.add_argument(
        "--delete",
        action="store_true",
        help="delete extra non-ignored files from the target before restoring",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            snapshot_dir = create_snapshot(
                args.root,
                store=args.store,
                name=args.name,
                workers=args.workers,
            )
            print(snapshot_dir)
            return 0
        if args.command == "list":
            for manifest in list_snapshots(args.root, store=args.store):
                print(_format_snapshot(manifest))
            return 0
        if args.command == "restore":
            target = restore_snapshot(
                args.snapshot,
                args.target,
                store=args.store,
                delete=args.delete,
                workers=args.workers,
            )
            print(target)
            return 0
    except SnapshotError as error:
        print(f"work_snapshot: {error}", file=sys.stderr)
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
