"""Materializace Agentis příloh do worktree a jejich `<attachments>` prompt sekce.

Přílohy přichází v ``context.attachments`` (task-level) nebo v ``attachments``
parametru ``add_message`` (přílohy feedback zprávy). Adapter je uloží jako
soubory do ``.agentis/attachments/`` ve worktree a do promptu přidá sekci
s cestami, aby si je agent mohl prohlédnout z disku. Používá to workflow runtime
(:class:`common.rpc.jsonrpc.AgentJsonRpcService`).
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import re
from pathlib import Path
from typing import Any

from common.adapter_base import log_json
from common.git_adapter import GitAdapterService

ATTACHMENTS_DIR = Path(".agentis/attachments")

_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


def attachment_field(attachment: Any, field_name: str) -> Any:
    if isinstance(attachment, dict):
        return attachment.get(field_name)
    return getattr(attachment, field_name, None)


def safe_attachment_filename(index: int, attachment: Any) -> str:
    raw_filename = attachment_field(attachment, "filename")
    raw_path = attachment_field(attachment, "path")
    raw_mime = attachment_field(attachment, "mime")

    filename = raw_filename if isinstance(raw_filename, str) and raw_filename.strip() else None
    if filename is None and isinstance(raw_path, str) and raw_path.strip():
        filename = Path(raw_path).name
    if filename is None:
        filename = f"attachment-{index}"

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).name).strip(".-")
    if not safe_name:
        safe_name = f"attachment-{index}"

    mime = raw_mime.strip().lower() if isinstance(raw_mime, str) and raw_mime.strip() else ""
    if "." not in safe_name and mime in _MIME_EXTENSIONS:
        safe_name = f"{safe_name}{_MIME_EXTENSIONS[mime]}"

    return f"{index:03d}-{safe_name}"


def decode_attachment_bytes(attachment: Any) -> bytes | None:
    content_base64 = attachment_field(attachment, "content_base64")
    if isinstance(content_base64, str) and content_base64.strip():
        try:
            return base64.b64decode(content_base64.strip(), validate=True)
        except (binascii.Error, ValueError):
            pass

    raw_path = attachment_field(attachment, "path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None

    file_path = Path(raw_path)
    if not file_path.exists() or not file_path.is_file():
        return None

    try:
        return file_path.read_bytes()
    except OSError:
        return None


def exclude_attachment_dir_from_git(worktree_path: Path, task_id: str | None = None) -> None:
    if not GitAdapterService._git_succeeds(worktree_path, "rev-parse", "--is-inside-work-tree"):
        return

    try:
        exclude_path = Path(GitAdapterService._run_git(worktree_path, "rev-parse", "--git-path", "info/exclude"))
        if not exclude_path.is_absolute():
            exclude_path = worktree_path / exclude_path
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        pattern = f"/{ATTACHMENTS_DIR.as_posix()}/"
        if pattern not in {line.strip() for line in existing.splitlines()}:
            with exclude_path.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(f"{pattern}\n")
    except Exception as exc:  # noqa: BLE001
        log_json(
            "WARN",
            "Failed to exclude attachments from git",
            task_id=task_id,
            error=str(exc),
        )


def next_attachment_index(worktree: str | Path) -> int:
    """Index pro další materializaci, aby follow-up zprávy nepřepsaly starší soubory."""
    target_dir = Path(worktree) / ATTACHMENTS_DIR
    if not target_dir.is_dir():
        return 1
    return sum(1 for item in target_dir.iterdir() if item.is_file()) + 1


def materialize_attachments(
    worktree: str | Path,
    attachments: list[Any] | None,
    *,
    task_id: str | None = None,
    start_index: int = 1,
) -> list[dict[str, str]]:
    if not attachments:
        return []

    worktree_path = Path(worktree)
    target_dir = worktree_path / ATTACHMENTS_DIR
    materialized: list[dict[str, str]] = []

    for index, attachment in enumerate(attachments, start=start_index):
        data = decode_attachment_bytes(attachment)
        if data is None:
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        filename = safe_attachment_filename(index, attachment)
        target_path = target_dir / filename
        try:
            target_path.write_bytes(data)
        except OSError:
            continue

        raw_mime = attachment_field(attachment, "mime")
        mime = raw_mime.strip() if isinstance(raw_mime, str) and raw_mime.strip() else None
        if mime is None:
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        materialized.append(
            {
                "filename": filename,
                "mime": mime,
                "path": str(target_path.relative_to(worktree_path)),
            }
        )

    if materialized:
        exclude_attachment_dir_from_git(worktree_path, task_id)

    return materialized


def build_attachments_block(attachments: list[dict[str, str]]) -> str | None:
    if not attachments:
        return None

    lines = [
        "<attachments>",
        "Agentis attachments were saved as local files. Use these paths when relevant; inspect image files from disk.",
    ]
    for index, attachment in enumerate(attachments, start=1):
        mime = attachment["mime"]
        kind = "image" if mime.startswith("image/") else "file"
        lines.append(f"{index}. {kind}: {attachment['filename']}")
        lines.append(f"path: {attachment['path']}")
        lines.append(f"mime: {mime}")
    lines.append("</attachments>")
    return "\n".join(lines)


__all__ = [
    "ATTACHMENTS_DIR",
    "attachment_field",
    "build_attachments_block",
    "decode_attachment_bytes",
    "exclude_attachment_dir_from_git",
    "materialize_attachments",
    "next_attachment_index",
    "safe_attachment_filename",
]
