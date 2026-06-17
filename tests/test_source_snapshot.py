from __future__ import annotations

import subprocess
import shutil
from pathlib import Path
from typing import Any

import pytest

from common.artifacts import source_snapshot


def test_snapshot_sources_uses_rsync_and_removes_previous_changes_diff(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    old_diff = worktree / source_snapshot.CHANGES_DIFF_NAME
    old_diff.write_text("previous diff", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    monkeypatch.setattr(
        source_snapshot.shutil, "which", lambda command: "/usr/bin/rsync" if command == "rsync" else None
    )

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
    monkeypatch.setattr(
        source_snapshot.shutil, "which", lambda command: "/usr/bin/rsync" if command == "rsync" else None
    )
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


def test_restore_source_snapshot_uses_rsync_without_delete_excluded(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    snapshot = tmp_path / "snapshots" / "snap-1" / "source"
    worktree.mkdir()
    snapshot.mkdir(parents=True)
    changes_diff = worktree / source_snapshot.CHANGES_DIFF_NAME
    changes_diff.write_text("diff", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    monkeypatch.setattr(
        source_snapshot.shutil, "which", lambda command: "/usr/bin/rsync" if command == "rsync" else None
    )

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(source_snapshot.subprocess, "run", fake_run)

    result = source_snapshot.restore_source_snapshot(worktree, "snap-1")

    assert result.status == "success"
    assert not changes_diff.exists()
    assert calls == [
        [
            "rsync",
            "-a",
            "--delete",
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
            f"{snapshot}/",
            f"{worktree}/",
        ]
    ]


def test_snapshot_sources_prefers_robocopy_on_windows(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktree"
    snapshot_dir = tmp_path / "snapshots" / "snap-1" / "source"
    worktree.mkdir()
    (worktree / ".gitignore").write_text(".env\n.venv/\n", encoding="utf-8")
    stale_git = snapshot_dir / ".git"
    stale_git.mkdir(parents=True)
    stale_env = snapshot_dir / ".env"
    stale_env.write_text("secret", encoding="utf-8")
    stale_venv = snapshot_dir / ".venv"
    stale_venv.mkdir()
    old_diff = worktree / source_snapshot.CHANGES_DIFF_NAME
    old_diff.write_text("previous diff", encoding="utf-8")
    calls: list[list[str]] = []
    envs: list[dict[str, str]] = []

    monkeypatch.setattr(source_snapshot, "_IS_WINDOWS", True)
    monkeypatch.setattr(source_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    monkeypatch.setattr(source_snapshot, "_robocopy_path", lambda path: f"WIN:{path}")
    monkeypatch.setattr(
        source_snapshot.shutil,
        "which",
        lambda command: "C:\\Windows\\System32\\robocopy.exe"
        if command == "robocopy"
        else "/usr/bin/rsync"
        if command == "rsync"
        else None,
    )
    monkeypatch.setenv("MSYS_NO_PATHCONV", "0")

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        envs.append(kwargs["env"])
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="copied", stderr="")

    monkeypatch.setattr(source_snapshot.subprocess, "run", fake_run)

    result = source_snapshot.snapshot_sources(worktree, "snap-1")

    assert result.status == "success"
    assert not old_diff.exists()
    assert not stale_git.exists()
    assert not stale_env.exists()
    assert not stale_venv.exists()
    assert envs[0]["MSYS_NO_PATHCONV"] == "1"
    assert calls == [
        [
            "robocopy",
            f"WIN:{worktree}",
            f"WIN:{snapshot_dir}",
            "/MIR",
            "/R:2",
            "/W:1",
            "/XJ",
            "/NP",
            "/XD",
            ".git",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".env",
            ".venv",
            "/XF",
            ".changes.diff",
            ".env",
        ]
    ]


def test_rsync_path_unchanged_on_posix(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(source_snapshot, "_IS_WINDOWS", False)
    assert source_snapshot._rsync_path(tmp_path / "worktree") == str(tmp_path / "worktree")


def test_rsync_path_uses_cygpath_on_windows(monkeypatch):
    monkeypatch.setattr(source_snapshot, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        source_snapshot.shutil, "which", lambda command: "/usr/bin/cygpath" if command == "cygpath" else None
    )

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ["/usr/bin/cygpath", "-u"]
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="/c/Ondrej/vscodium/vscode\n", stderr="")

    monkeypatch.setattr(source_snapshot.subprocess, "run", fake_run)

    assert source_snapshot._rsync_path(Path(r"C:\Ondrej\vscodium\vscode")) == "/c/Ondrej/vscodium/vscode"


def test_rsync_path_prefers_cygpath_next_to_rsync(monkeypatch, tmp_path: Path):
    # rsync z cygwinu mapuje disky jinak (/cygdrive/c) než Git Bash cygpath na PATH (/c),
    # takže se musí použít cygpath ze stejného adresáře jako rsync.
    bindir = tmp_path / "cygwin" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "rsync").write_text("", encoding="utf-8")
    (bindir / "cygpath").write_text("", encoding="utf-8")

    monkeypatch.setattr(source_snapshot, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        source_snapshot.shutil,
        "which",
        lambda command: str(bindir / "rsync") if command == "rsync" else "/usr/bin/cygpath",
    )

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert args[0] == str(bindir / "cygpath")
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="/cygdrive/c/Ondrej/vscodium/vscode\n", stderr=""
        )

    monkeypatch.setattr(source_snapshot.subprocess, "run", fake_run)

    assert source_snapshot._rsync_path(Path(r"C:\Ondrej\vscodium\vscode")) == "/cygdrive/c/Ondrej/vscodium/vscode"


def test_rsync_path_falls_back_to_drive_form_without_cygpath(monkeypatch):
    monkeypatch.setattr(source_snapshot, "_IS_WINDOWS", True)
    monkeypatch.setattr(source_snapshot.shutil, "which", lambda command: None)

    # Bez cygpathu nesmí padnout do ssh host:path tvaru `C:...`.
    assert source_snapshot._rsync_path(Path(r"C:\Ondrej\vscodium\vscode")) == "/c/Ondrej/vscodium/vscode"


def test_detect_windows_covers_cygwin_and_msys(monkeypatch):
    # Git Bash spouští adapter v cygwin/MSYS Pythonu, kde os.name == "posix".
    monkeypatch.setattr(source_snapshot.os, "name", "posix")
    for platform in ("msys", "cygwin"):
        monkeypatch.setattr(source_snapshot.sys, "platform", platform)
        assert source_snapshot._detect_windows() is True
    monkeypatch.setattr(source_snapshot.sys, "platform", "linux")
    assert source_snapshot._detect_windows() is False
    monkeypatch.setattr(source_snapshot.os, "name", "nt")
    assert source_snapshot._detect_windows() is True


def test_rsync_path_converts_drive_form_even_when_not_flagged_windows(monkeypatch):
    # Pod cygwin/MSYS Pythonem (_IS_WINDOWS klidně False) musí drive cesta worktree
    # projít převodem, jinak ji MSYS přemapuje na špatný mount.
    monkeypatch.setattr(source_snapshot, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        source_snapshot.shutil, "which", lambda command: "/usr/bin/cygpath" if command == "cygpath" else None
    )

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ["/usr/bin/cygpath", "-u"]
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="/cygdrive/c/Ondrej/vscodium/vscode\n", stderr=""
        )

    monkeypatch.setattr(source_snapshot.subprocess, "run", fake_run)

    assert source_snapshot._rsync_path(Path(r"C:\Ondrej\vscodium\vscode")) == "/cygdrive/c/Ondrej/vscodium/vscode"


def test_rsync_path_passes_through_posix_snapshot_root(monkeypatch):
    # Snapshot root v POSIX tvaru (`/tmp/...`) se nesmí na Windows zmršit.
    monkeypatch.setattr(source_snapshot, "_IS_WINDOWS", True)
    path = "/tmp/agentis-source-snapshots/workflow-x/source"
    assert source_snapshot._rsync_path(Path(path)) == path


def test_windows_drive_path_anchors_driveless_via_cygpath(monkeypatch):
    monkeypatch.setattr(
        source_snapshot.shutil, "which", lambda command: "/usr/bin/cygpath" if command == "cygpath" else None
    )

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ["/usr/bin/cygpath", "-w"]
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="C:\\msys64\\tmp\n", stderr="")

    monkeypatch.setattr(source_snapshot.subprocess, "run", fake_run)

    assert source_snapshot._windows_drive_path("/tmp") == "C:/msys64/tmp"


def test_windows_drive_path_normalizes_existing_drive(monkeypatch):
    # Disk-qualified temp už cygpath nepotřebuje, jen sjednotí slashe.
    monkeypatch.setattr(
        source_snapshot.subprocess, "run", lambda *a, **k: pytest.fail("cygpath must not run for drive path")
    )

    assert source_snapshot._windows_drive_path(r"C:\Users\x\AppData\Local\Temp") == "C:/Users/x/AppData/Local/Temp"


def test_windows_drive_path_uses_system_drive_without_cygpath(monkeypatch):
    monkeypatch.setattr(source_snapshot.shutil, "which", lambda command: None)
    for env_name in ("TEMP", "TMP", "LOCALAPPDATA", "USERPROFILE"):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("SystemDrive", "D:")

    assert source_snapshot._windows_drive_path("/tmp") == "D:/tmp"


def test_windows_drive_path_converts_msys_drive_without_cygpath(monkeypatch):
    monkeypatch.setattr(source_snapshot.shutil, "which", lambda command: None)

    assert source_snapshot._windows_drive_path("/c/Ondrej/vscodium/vscode") == "C:/Ondrej/vscodium/vscode"
    assert source_snapshot._windows_drive_path("/cygdrive/d/tmp") == "D:/tmp"


def test_robocopy_path_keeps_native_windows_drive(monkeypatch):
    monkeypatch.setattr(
        source_snapshot.subprocess, "run", lambda *a, **k: pytest.fail("cygpath must not run for drive path")
    )

    assert source_snapshot._robocopy_path(Path(r"C:\Ondrej\vscodium\vscode")) == r"C:\Ondrej\vscodium\vscode"


def test_robocopy_excludes_include_root_gitignore(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".gitignore").write_text(
        "\n".join(
            [
                "# comment",
                ".env",
                ".venv/",
                "dist",
                "opencode-container/registry-secret.yaml",
                "!keep.env",
            ]
        ),
        encoding="utf-8",
    )

    dir_excludes, file_excludes = source_snapshot._robocopy_excludes(source)

    assert ".git" in dir_excludes
    assert ".env" in dir_excludes
    assert ".venv" in dir_excludes
    assert "dist" in dir_excludes
    assert ".changes.diff" in file_excludes
    assert ".env" in file_excludes
    assert "dist" in file_excludes
    assert r"opencode-container\registry-secret.yaml" in file_excludes
    assert "keep.env" not in file_excludes


def test_rsync_path_ignores_foreign_path_cygpath_for_cygwin_rsync(monkeypatch, tmp_path: Path):
    # Když je rsync z cwRsync/Cygwin a cygpath jen z Git Bash PATH, `/c/...` by rsync neviděl.
    bindir = tmp_path / "cwrsync" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "rsync.exe").write_text("", encoding="utf-8")
    (bindir / "cygwin1.dll").write_text("", encoding="utf-8")

    monkeypatch.setattr(source_snapshot, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        source_snapshot.shutil,
        "which",
        lambda command: str(bindir / "rsync.exe") if command == "rsync" else "/usr/bin/cygpath",
    )
    monkeypatch.setattr(
        source_snapshot.subprocess,
        "run",
        lambda *a, **k: pytest.fail("foreign cygpath must not run for rsync paths"),
    )

    assert source_snapshot._rsync_path(Path(r"C:\Ondrej\vscodium\vscode")) == "/cygdrive/c/Ondrej/vscodium/vscode"


def test_rsync_path_fallback_uses_cygdrive_for_cygwin_rsync(monkeypatch, tmp_path: Path):
    # cwRsync/cygwin rsync nemusí mít cygpath, ale pozná se podle cygwin1.dll vedle
    # rsync.exe → disky se mapují na /cygdrive/c, ne /c.
    bindir = tmp_path / "cwrsync" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "rsync.exe").write_text("", encoding="utf-8")
    (bindir / "cygwin1.dll").write_text("", encoding="utf-8")

    monkeypatch.setattr(source_snapshot, "_IS_WINDOWS", True)
    # cygpath nikde (ani vedle rsync, ani v PATH) → spadne do fallbacku.
    monkeypatch.setattr(
        source_snapshot.shutil,
        "which",
        lambda command: str(bindir / "rsync.exe") if command == "rsync" else None,
    )

    assert source_snapshot._rsync_path(Path(r"C:\Ondrej\vscodium\vscode")) == "/cygdrive/c/Ondrej/vscodium/vscode"


def _read_simple_gitignore(source: Path) -> set[str]:
    gitignore = source / ".gitignore"
    if not gitignore.is_file():
        return set()
    return {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines() if line.strip()}
