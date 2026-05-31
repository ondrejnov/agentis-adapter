"""Slack text helpers.

Normalize inbound Slack message text (strip mentions, unwrap links), turn a
thread into a plain-text context block, and convert plain text into the Lexical
document shape Agentis stores for task descriptions.
"""

from __future__ import annotations

import re


MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
LINK_RE = re.compile(r"<([^>|]+)\|([^>]+)>")
RAW_LINK_RE = re.compile(r"<([^>|]+)>")
QUESTION_ANSWER_RE = re.compile(r"^answer\s+(?P<id>\S+)\s*:\s*(?P<answer>.+)$", flags=re.IGNORECASE | re.DOTALL)


def normalize_slack_text(text: str, *, bot_user_id: str | None = None) -> str:
    """Strip the bot mention and Slack link markup, collapse whitespace."""
    if bot_user_id:
        text = text.replace(f"<@{bot_user_id}>", "")
    text = MENTION_RE.sub("", text)
    text = LINK_RE.sub(lambda match: f"{match.group(2)} ({match.group(1)})", text)
    text = RAW_LINK_RE.sub(lambda match: match.group(1), text)
    return " ".join(text.split()).strip()


def slack_history_to_context(messages: list[dict]) -> str:
    """Render a Slack thread as ``[Author] text`` lines for the task body."""
    lines: list[str] = []
    for message in messages:
        profile = message.get("user_profile") or {}
        user = (
            message.get("user_real_name")
            or profile.get("real_name")
            or profile.get("display_name")
            or message.get("username")
            or message.get("user")
            or "unknown"
        )
        text = normalize_slack_text(str(message.get("text") or ""))
        if text:
            lines.append(f"[{user}] {text}")
    return "\n".join(lines)


def plaintext_to_lexical(text: str) -> dict:
    """Wrap plain text in the minimal Lexical document Agentis expects."""
    return {
        "root": {
            "type": "root",
            "format": "",
            "indent": 0,
            "version": 1,
            "children": [
                {
                    "type": "paragraph",
                    "format": "",
                    "indent": 0,
                    "version": 1,
                    "children": [
                        {
                            "type": "text",
                            "text": text,
                            "format": 0,
                            "style": "",
                            "mode": "normal",
                            "detail": 0,
                            "version": 1,
                        }
                    ],
                    "direction": None,
                    "textFormat": 0,
                    "textStyle": "",
                }
            ],
            "direction": None,
        }
    }


def parse_question_answer(text: str) -> tuple[str, str] | None:
    """Parse ``answer <external_id>: <answer>`` replies; ``None`` if it is not one."""
    match = QUESTION_ANSWER_RE.match(text.strip())
    if not match:
        return None
    return match.group("id"), match.group("answer").strip()
