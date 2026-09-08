from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


ROLE_DIR_RELPATH = Path(".agentis/role")
_MAX_ROLE_FILE_SIZE = 256 * 1024


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _without_attachments(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_attachments(item) for key, item in value.items() if key != "attachments"}
    if isinstance(value, list):
        return [_without_attachments(item) for item in value]
    return value


def _role_file(path: Path) -> tuple[str | None, str] | None:
    try:
        if path.stat().st_size > _MAX_ROLE_FILE_SIZE:
            return None
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None

    if not content.startswith("---"):
        return None, content.strip()

    lines = content.splitlines()
    try:
        closing_index = lines.index("---", 1)
        metadata = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
    except (ValueError, yaml.YAMLError):
        return None
    if not isinstance(metadata, dict):
        return None

    name = metadata.get("name")
    return (name.strip() if isinstance(name, str) and name.strip() else None), "\n".join(
        lines[closing_index + 1 :]
    ).strip()


def load_repository_role(worktree: str | Path, role_name: str) -> str | None:
    """Načte tělo projektové role, přičemž nedovolí následovat symlink mimo adresář rolí."""
    if not role_name.strip():
        return None

    role_dir = Path(worktree) / ROLE_DIR_RELPATH
    try:
        resolved_worktree = Path(worktree).resolve(strict=True)
        resolved_dir = role_dir.resolve(strict=True)
        if not resolved_dir.is_relative_to(resolved_worktree):
            return None
        candidates = sorted(role_dir.glob("*.md"))
    except OSError:
        return None

    exact_filename = f"{role_name.strip()}.md"
    fallback: str | None = None
    for candidate in candidates:
        try:
            resolved_candidate = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved_candidate.is_relative_to(resolved_dir) or not resolved_candidate.is_file():
            continue

        parsed = _role_file(resolved_candidate)
        if parsed is None:
            continue
        declared_name, body = parsed
        if candidate.name == exact_filename:
            fallback = body or None
        if declared_name == role_name.strip() and body:
            return body
    return fallback


def build_comments_block(comments: list[Any] | None) -> str | None:
    if not comments:
        return None

    entries: list[str] = []
    for index, comment in enumerate(comments, start=1):
        body = _field(comment, "body")
        if not isinstance(body, str) or not body.strip():
            continue

        author_name = _field(comment, "author_name")
        author_type = _field(comment, "author_type")
        created = _field(comment, "created")

        meta_parts: list[str] = []
        if isinstance(author_name, str) and author_name.strip():
            meta_parts.append(author_name.strip())
        elif isinstance(author_type, str) and author_type.strip():
            meta_parts.append(author_type.strip())
        if isinstance(created, str) and created.strip():
            meta_parts.append(created.strip())

        header = f"{index}."
        if meta_parts:
            header = f"{header} {' | '.join(meta_parts)}"
        entries.append(f"{header}\n{body.strip()}")

    if not entries:
        return None
    return "<comments>\n" + "\n\n".join(entries) + "\n</comments>"


def build_parent_task_block(parent_task: Any | None) -> str | None:
    if parent_task is None:
        return None

    if hasattr(parent_task, "model_dump"):
        payload = parent_task.model_dump(mode="json")
    elif isinstance(parent_task, dict):
        payload = parent_task
    else:
        return None

    payload = _without_attachments(payload)
    return "<parent_task_context>\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n</parent_task_context>"
