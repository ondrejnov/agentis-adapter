from __future__ import annotations

import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


def _detect_windows() -> bool:
    """Windows i v cygwin/MSYS Pythonu (Git Bash), kde `os.name == "posix"`."""

    return os.name == "nt" or sys.platform.startswith(("cygwin", "msys"))


_IS_WINDOWS = _detect_windows()


def _rsync_dir() -> Path | None:
    rsync = shutil.which("rsync")
    if not rsync:
        return None
    return Path(rsync).parent


def _rsync_uses_cygwin_mounts() -> bool:
    rsync_dir = _rsync_dir()
    return bool(rsync_dir and (rsync_dir / "cygwin1.dll").exists())


def _cygpath_tool(*, for_rsync: bool) -> str | None:
    """`cygpath` ze stejné sady nástrojů jako rsync.

    Mount konvence je per-toolchain: Git Bash/MSYS2 mapuje disky na `/c/...`,
    cygwin/cwRsync na `/cygdrive/c/...`. cygpath z *jiného* balíku než rsync by
    vyrobil cestu, kterou rsync nenajde (`change_dir "/c/..." failed`). Hledáme
    proto cygpath nejdřív vedle rsync executable. U Cygwin/cwRsync bez vlastního
    cygpathu nesmíme pro rsync argument použít cizí Git Bash cygpath z PATH.
    """

    rsync_dir = _rsync_dir()
    if rsync_dir:
        for name in ("cygpath.exe", "cygpath"):
            candidate = rsync_dir / name
            if candidate.is_file():
                return str(candidate)
        if for_rsync and _rsync_uses_cygwin_mounts():
            return None
    return shutil.which("cygpath")


def _drive_mount_prefix() -> str:
    """Mount prefix disků pro fallback, když chybí cygpath.

    cygwin/cwRsync (běží vedle `cygwin1.dll`) mapuje disky na `/cygdrive/c`,
    MSYS2/Git Bash (vedle `msys-2.0.dll`) na `/c`. Rozlišíme to podle DLL u
    rsync executable; default je MSYS2 tvar.
    """

    if _rsync_uses_cygwin_mounts():
        return "/cygdrive"
    return ""


def _cygpath(raw: str, flag: str, *, for_rsync: bool = True) -> str | None:
    """Převod cesty přes `cygpath` z toolchainu u rsync; None když chybí/selže."""

    tool = _cygpath_tool(for_rsync=for_rsync)
    if not tool:
        return None
    with suppress(OSError, subprocess.SubprocessError):
        completed = subprocess.run([tool, flag, raw], capture_output=True, text=True, check=True)
        converted = completed.stdout.strip()
        if converted:
            return converted
    return None


def _windows_drive_path(raw: str) -> str:
    """Driveless temp (`/tmp`) ukotvený na konkrétní disk, s forward slashy.

    Forward slashe držíme schválně: `Path` joiny i `cygpath -u` je zvládnou
    čistě i pod cygwin/MSYS Pythonem (kde `Path` je POSIX a backslash by zůstal
    literálem). Disk-qualified vstup (`C:\\Users\\...\\Temp`) jen znormalizujeme,
    driveless přeženeme přes `cygpath -w`; bez cygpathu použijeme diskový fallback.
    """

    if ntpath.splitdrive(raw)[0]:
        return raw.replace("\\", "/")
    win = _cygpath(raw, "-w", for_rsync=False)
    if win and ntpath.splitdrive(win)[0]:
        return win.replace("\\", "/")
    return _windows_drive_path_fallback(raw)


def _windows_drive_path_fallback(raw: str) -> str:
    """Nouzově ukotví POSIX temp na Windows disk, když `cygpath -w` není k dispozici."""

    candidates = [os.environ.get("TEMP"), os.environ.get("TMP")]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(ntpath.join(local_app_data, "Temp"))
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        candidates.append(ntpath.join(user_profile, "AppData", "Local", "Temp"))

    for candidate in candidates:
        if candidate and ntpath.splitdrive(candidate)[0]:
            return candidate.replace("\\", "/")

    drive = os.environ.get("SystemDrive") or ntpath.splitdrive(os.getcwd())[0]
    if len(drive) == 2 and drive.endswith(":"):
        tail = raw.replace("\\", "/").lstrip("/") or "tmp"
        return f"{drive}/{tail}"
    return raw


def _temp_root() -> Path:
    """Reálný, na disk ukotvený temp adresář — společný pro Python i rsync.

    Na Windows běží adapter typicky z Git Bash, kde `tempfile.gettempdir()`
    vrátí *driveless* `/tmp` (TMP=/tmp). Takovou cestu si nativní Python mapuje
    na aktuální disk (`C:\\tmp`), kdežto rsync z cygwinu/MSYS na úplně jiný
    reálný adresář (`C:\\cygwin64\\tmp` resp. `C:\\msys64\\tmp`) — Python tedy
    založí parent jinde, než kam rsync zapisuje (`mkdir ... failed:
    No such file or directory`). Cestu proto ukotvíme na konkrétní disk přes
    `cygpath -w` nebo diskový fallback; obě strany ji pak (po `_rsync_path`) vidí
    jako tentýž adresář. Na POSIX necháváme `/tmp`.
    """

    raw = tempfile.gettempdir()
    if not _IS_WINDOWS:
        return Path(raw)
    return Path(_windows_drive_path(raw))


SNAPSHOT_ROOT = _temp_root() / "agentis-source-snapshots"
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
    args.extend([f"{_rsync_path(source_dir)}/", f"{_rsync_path(target_dir)}/"])
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _rsync_restore_filtered(source_dir: Path, target_dir: Path) -> subprocess.CompletedProcess[str]:
    args = ["rsync", "-a", "--delete", "--filter", ":- .gitignore"]
    for pattern in _EXCLUDES:
        args.extend(["--exclude", pattern])
    args.extend([f"{_rsync_path(source_dir)}/", f"{_rsync_path(target_dir)}/"])
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _is_windows_path(raw: str) -> bool:
    """Cesta, kterou rsync nepřečte bez převodu na mount tvar (`/c/…`).

    Drive letter (`C:\\…`, `C:/…`) je jednoznačný signál nezávislý na `os.name`,
    takže worktree v nativním Windows tvaru převedeme i pod cygwin/MSYS Pythonem
    (kde `os.name == "posix"`). Backslash bez disku řešíme jen na Windows, ať na
    POSIXu nemrvíme legitimní jména souborů.
    """

    if ntpath.splitdrive(raw)[0]:
        return True
    return _IS_WINDOWS and "\\" in raw


def _rsync_path(path: Path) -> str:
    """Cesta do rsync argumentu, bezpečná i na Windows.

    Worktree má na Windows drive-letter cestu (`C:\\Ondrej\\...`); rsync build z
    Git Bash/MSYS2/cygwin čte `C:` jako `host:path` a spustí ssh
    (`Could not resolve hostname c`), nebo si ji MSYS sám přemapuje na špatný
    mount. Převedeme proto cestu na POSIX mount form přes `cygpath -u` z téhož
    balíku jako rsync (`_cygpath`), aby seděl prefix (`/c/...` MSYS2 vs
    `/cygdrive/c/...` cygwin). Když cygpath chybí, prefix uhádneme z DLL u rsync
    (`_drive_mount_prefix`). POSIX cesty (`/tmp/...`) vracíme beze změny.
    """

    raw = str(path)
    if not _is_windows_path(raw):
        return raw
    converted = _cygpath(raw, "-u")
    if converted:
        return converted
    drive, tail = ntpath.splitdrive(raw)
    tail = tail.replace("\\", "/")
    if len(drive) == 2 and drive.endswith(":"):
        return f"{_drive_mount_prefix()}/{drive[0].lower()}{tail}"
    return raw.replace("\\", "/")


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
