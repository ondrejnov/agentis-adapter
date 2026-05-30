from __future__ import annotations

import base64
from pathlib import Path
from typing import Any


class OpenCodeUtils:
    @staticmethod
    def _attachment_field(attachment: Any, field_name: str) -> Any:
        if isinstance(attachment, dict):
            return attachment.get(field_name)
        return getattr(attachment, field_name, None)

    @staticmethod
    def _comment_field(comment: Any, field_name: str) -> Any:
        if isinstance(comment, dict):
            return comment.get(field_name)
        return getattr(comment, field_name, None)

    @staticmethod
    def extract_message_text(message: Any) -> str:
        if isinstance(message, str):
            return message.strip()

        if not isinstance(message, dict):
            return ""

        parts = message.get("parts")
        if not isinstance(parts, list):
            return ""

        text_parts = [
            text.strip()
            for part in parts
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(text := part.get("text"), str)
            and text.strip()
        ]
        return "\n\n".join(text_parts)

    @staticmethod
    def build_text_parts(*texts: str | None) -> list[dict[str, str]]:
        """Build an OpenCode `parts` array from one or more text chunks.

        Empty/None values and duplicates of the preceding chunk are skipped.
        """
        chunks: list[str] = []
        for text in texts:
            if not isinstance(text, str):
                continue
            stripped = text.strip()
            if not stripped:
                continue
            if chunks and chunks[-1] == stripped:
                continue
            chunks.append(stripped)

        joined = "\n\n".join(chunks)
        if not joined:
            return []
        return [{"type": "text", "text": joined}]

    @classmethod
    def build_attachments_parts(cls, attachments: list[Any] | None) -> list[dict[str, str]]:
        parts: list[dict[str, str]] = []
        if not attachments:
            return parts

        for attachment in attachments:
            content_base64 = cls._attachment_field(attachment, "content_base64")
            path = cls._attachment_field(attachment, "path")
            raw_filename = cls._attachment_field(attachment, "filename")
            raw_mime = cls._attachment_field(attachment, "mime")

            filename = raw_filename.strip() if isinstance(raw_filename, str) and raw_filename.strip() else None
            mime = raw_mime.strip() if isinstance(raw_mime, str) and raw_mime.strip() else None

            if isinstance(content_base64, str) and content_base64.strip():
                fallback_name = Path(path).name if isinstance(path, str) and path.strip() else "attachment"
                part_filename = filename or fallback_name
                part_mime = mime or cls.guess_mime(part_filename)
                parts.append(
                    {
                        "type": "file",
                        "mime": part_mime,
                        "filename": part_filename,
                        "url": f"data:{part_mime};base64,{content_base64.strip()}",
                    }
                )
                continue

            if not isinstance(path, str) or not path.strip():
                continue

            file_path = Path(path)
            if not file_path.exists() or not file_path.is_file():
                continue

            try:
                encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
            except OSError:
                continue

            part_filename = filename or file_path.name
            part_mime = mime or cls.guess_mime(part_filename)
            parts.append(
                {
                    "type": "file",
                    "mime": part_mime,
                    "filename": part_filename,
                    "url": f"data:{part_mime};base64,{encoded}",
                }
            )

        return parts

    @staticmethod
    def guess_mime(filename: str) -> str:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in {"txt", "md", "csv", "log"}:
            return "text/plain"
        if ext == "json":
            return "application/json"
        if ext == "pdf":
            return "application/pdf"
        if ext in {"doc", "docx"}:
            return "application/msword"
        if ext in {"xls", "xlsx"}:
            return "application/vnd.ms-excel"
        if ext == "png":
            return "image/png"
        if ext in {"jpg", "jpeg"}:
            return "image/jpeg"
        if ext == "gif":
            return "image/gif"
        if ext == "svg":
            return "image/svg+xml"
        if ext == "zip":
            return "application/zip"
        return "application/octet-stream"

    @classmethod
    def build_comments_block(cls, comments: list[Any] | None) -> str | None:
        if not comments:
            return None

        entries: list[str] = []
        for index, comment in enumerate(comments, start=1):
            body = cls._comment_field(comment, "body")
            if not isinstance(body, str) or not body.strip():
                continue

            author_name = cls._comment_field(comment, "author_name")
            author_type = cls._comment_field(comment, "author_type")
            created = cls._comment_field(comment, "created")

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

    @staticmethod
    def parse_model(raw: str | None) -> dict[str, str] | None:
        """Parse `"providerID/modelID"` into the nested OpenCode model object."""
        if not isinstance(raw, str):
            return None
        stripped = raw.strip()
        if not stripped:
            return None
        if "/" in stripped:
            provider_id, model_id = stripped.split("/", 1)
            provider_id = provider_id.strip()
            model_id = model_id.strip()
            if not model_id:
                return None
            if provider_id:
                return {"providerID": provider_id, "modelID": model_id}
            return {"modelID": model_id}
        return {"modelID": stripped}

    @staticmethod
    def extract_session_id(response: Any) -> str | None:
        """Return session ID from an OpenCode session response payload."""
        if not isinstance(response, dict):
            return None
        for key in ("id", "sessionID"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
