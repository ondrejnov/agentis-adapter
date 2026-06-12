#!/usr/bin/env python3
"""Tee JSON Lines eventů agenta + průběžná editace pending zprávy ve Slacku.

Použití ve workflow kroku (viz `.agentis/workflows/slack.yaml`):

    agentiscode --json ... | python3 scripts/slack_stream.py

Stdin (JSON Lines z `agentiscode --json`) se beze změny propouští na stdout,
takže log kroku zůstává kompletní. Po cestě se z eventů skládá jednořádkový
stav („právě běží nástroj X“, poslední reasoning) a throttlovaně se jím
edituje pending zpráva ve Slacku přes `chat.update`.

Konfigurace přes env (chybějící hodnoty = čistý tee, žádné volání Slacku):

- ``TASK_HEADER_SLACK_CHANNEL`` / ``TASK_HEADER_SLACK_MESSAGE_TS`` — adresát,
  ts pending zprávy posílá bridge v task headers,
- ``SLACK_BOT_TOKEN`` — ze sourcovaného ``slack.env``,
- ``SLACK_STREAM_INTERVAL`` — minimální odstup editací v sekundách (default 3;
  chat.update je Slack rate-limit Tier 3, ~50/min, default drží ~20/min).

Updater nikdy neshazuje pipeline: chyby Slacku jen loguje na stderr a stream
propouští dál. Finální odpověď do pending zprávy zapisuje až následný krok
workflow — tady se řeší jen průběh.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

PENDING_PREFIX = "⏳ _Pracuju na tom…_"
SNIPPET_LIMIT = 150


def _post_update(channel: str, ts: str, token: str, text: str) -> None:
    payload = {"channel": channel, "ts": ts, "text": text}
    request = urllib.request.Request(
        "https://slack.com/api/chat.update",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            print(f"slack-stream: chat.update odmítnut: {body.get('error')}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — updater nesmí shodit běh agenta
        print(f"slack-stream: chat.update selhal: {exc}", file=sys.stderr)


def _status_from_event(event_type: str, data: dict) -> str | None:
    if event_type == "tool" and data.get("status") == "running":
        name = data.get("name") or "tool"
        title = data.get("title") or ""
        suffix = f" {title[:SNIPPET_LIMIT]}" if title and title != name else ""
        return f"{PENDING_PREFIX} ⚙ `{name}`{suffix}"
    if event_type == "reasoning":
        text = (data.get("text") or "").strip()
        if text:
            return f"{PENDING_PREFIX} 💭 {text.splitlines()[0][:SNIPPET_LIMIT]}"
    if event_type == "error":
        message = (data.get("message") or "chyba").strip()
        return f"{PENDING_PREFIX} ✗ {message.splitlines()[0][:SNIPPET_LIMIT]}"
    return None


def main() -> int:
    channel = os.environ.get("TASK_HEADER_SLACK_CHANNEL", "")
    message_ts = os.environ.get("TASK_HEADER_SLACK_MESSAGE_TS", "")
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    min_interval = float(os.environ.get("SLACK_STREAM_INTERVAL", "3"))
    enabled = bool(channel and message_ts and token)
    if not enabled:
        print("slack-stream: chybí channel/ts/token, běžím jen jako tee", file=sys.stderr)

    pending: str | None = None
    last_sent = 0.0
    for line in sys.stdin:
        sys.stdout.write(line)
        sys.stdout.flush()
        if not enabled:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        status = _status_from_event(event.get("type") or "", event.get("data") or {})
        if status:
            pending = status
        if pending and time.monotonic() - last_sent >= min_interval:
            _post_update(channel, message_ts, token, pending)
            last_sent = time.monotonic()
            pending = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
