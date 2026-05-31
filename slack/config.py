"""Slack-specific configuration.

The Agentis connection (endpoint + service token, reconnect knobs …) lives in
:class:`common.config.Settings`; this module only adds the Slack-side knobs:
the Slack tokens and the defaults stamped onto tasks created from mentions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# Agentis label applied to every task created from a Slack mention so they are
# easy to find/filter. Overridable via ``SLACK_TASK_LABEL_ID``.
DEFAULT_SLACK_TASK_LABEL_ID = "019e4eb9-6a52-7bbd-bcd6-fd9f7482263a"


def _load_env_file(path: str | os.PathLike[str]) -> None:
    """Best-effort ``.env`` loader: set keys that are not already in the env."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


@dataclass(frozen=True)
class SlackSettings:
    slack_bot_token: str
    slack_app_token: str
    default_project: str | None
    default_agent: str | None
    default_model: str | None
    default_effort: str | None
    default_adapter: str | None
    default_adapter_engine: str | None
    default_environment: str | None
    task_label_id: str | None
    rate_limit_window_seconds: int
    rate_limit_max_events: int
    thread_history_limit: int

    @classmethod
    def from_env(cls) -> "SlackSettings":
        project_root = Path(__file__).parent.parent.resolve()
        _load_env_file(project_root / ".env")
        return cls(
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
            slack_app_token=os.getenv("SLACK_APP_TOKEN", ""),
            default_project=os.getenv("SLACK_DEFAULT_PROJECT") or None,
            default_agent=os.getenv("SLACK_DEFAULT_AGENT") or None,
            default_model=os.getenv("SLACK_DEFAULT_MODEL") or None,
            default_effort=os.getenv("SLACK_DEFAULT_EFFORT") or None,
            default_adapter=os.getenv("SLACK_DEFAULT_ADAPTER", "claude") or None,
            default_adapter_engine=os.getenv("SLACK_DEFAULT_ADAPTER_ENGINE") or None,
            default_environment=os.getenv("SLACK_DEFAULT_ENVIRONMENT") or None,
            task_label_id=os.getenv("SLACK_TASK_LABEL_ID", DEFAULT_SLACK_TASK_LABEL_ID) or None,
            rate_limit_window_seconds=int(os.getenv("SLACK_RATE_LIMIT_WINDOW_SECONDS", "60")),
            rate_limit_max_events=int(os.getenv("SLACK_RATE_LIMIT_MAX_EVENTS", "30")),
            thread_history_limit=int(os.getenv("SLACK_THREAD_HISTORY_LIMIT", "200")),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in {
                "SLACK_BOT_TOKEN": self.slack_bot_token,
                "SLACK_APP_TOKEN": self.slack_app_token,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
