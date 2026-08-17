from __future__ import annotations

from typing import Any


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


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
