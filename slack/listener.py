"""Turn Slack bot mentions into Agentis tasks and runs.

:class:`SlackMentionService` is the heart of the Slack adapter. It is wired to a
Slack Web API client (for thread history / reactions) and a
:class:`~slack.agentis_tasks.SlackAgentisGateway` (for task creation). It is
deliberately transport-agnostic — ``app_mention``/``message`` events are passed
in as plain dicts — so it can be unit-tested without a live Slack connection.
"""

from __future__ import annotations

from typing import Any

from slack.agentis_tasks import SlackAgentisGateway
from slack.config import SlackSettings
from slack.guards import EventDeduper, GlobalRateLimiter, should_ignore_event
from slack.text import normalize_slack_text, parse_question_answer, plaintext_to_lexical, slack_history_to_context


class SlackMentionService:
    def __init__(
        self,
        *,
        settings: SlackSettings,
        agentis: SlackAgentisGateway,
        slack_client: Any,
        bot_user_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.agentis = agentis
        self.slack_client = slack_client
        self.bot_user_id = bot_user_id
        self.deduper = EventDeduper()
        self.rate_limiter = GlobalRateLimiter(settings.rate_limit_max_events, settings.rate_limit_window_seconds)

    def handle_app_mention(self, event: dict[str, Any], *, event_id: str | None = None) -> dict[str, Any]:
        if should_ignore_event(event, bot_user_id=self.bot_user_id):
            return {"ignored": True, "reason": "bot"}
        dedupe_key = event_id or f"{event.get('team')}:{event.get('channel')}:{event.get('ts')}"
        if self.deduper.seen_before(dedupe_key):
            return {"ignored": True, "reason": "duplicate"}
        if not self.rate_limiter.allow():
            return {"ignored": True, "reason": "rate_limited"}

        team_id = str(event.get("team") or event.get("team_id") or "")
        channel_id = str(event.get("channel") or "")
        message_ts = str(event.get("ts") or "")
        thread_ts = str(event.get("thread_ts") or message_ts)
        text = normalize_slack_text(str(event.get("text") or ""), bot_user_id=self.bot_user_id)

        duplicate = self.agentis.find_by_external_ref(
            {
                "source": "slack",
                "slack.team_id": team_id,
                "slack.channel_id": channel_id,
                "slack.message_ts": message_ts,
            }
        )
        if duplicate:
            return {"ignored": True, "reason": "duplicate_task", "task": duplicate}

        history = self.fetch_thread_history(channel_id, thread_ts)
        headers = self.build_headers(event, thread_ts=thread_ts)
        context_text = slack_history_to_context(history)
        body = text if not context_text else f"{text}\n\nSlack thread history:\n{context_text}"

        task_data = {
            "title": text[:80] or "Slack mention",
            "description": plaintext_to_lexical(body),
            "headers": headers,
            "project": self.settings.default_project,
            "agent": self.settings.default_agent,
            "model": {"id": self.settings.default_model, "effort": self.settings.default_effort}
            if self.settings.default_model
            else None,
            "adapter": self.settings.default_adapter,
            "adapter_options": {"engine": self.settings.default_adapter_engine}
            if self.settings.default_adapter_engine
            else None,
            "environment": self.settings.default_environment,
            "labels": [self.settings.task_label_id] if self.settings.task_label_id else None,
            "worktree": True,
        }
        saved = self.agentis.save_task({key: value for key, value in task_data.items() if value is not None})
        task_id = saved["form"]["id"]
        run = self.agentis.start_run(task_id, start_adapter=True)
        self.slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="eyes")
        return {"created": True, "task": saved.get("form"), "run": run}

    def handle_message(self, event: dict[str, Any]) -> dict[str, Any]:
        if should_ignore_event(event, bot_user_id=self.bot_user_id):
            return {"ignored": True, "reason": "bot"}
        parsed = parse_question_answer(str(event.get("text") or ""))
        if not parsed:
            return {"ignored": True, "reason": "not_question_answer"}
        external_id, answer = parsed
        result = self.agentis.question_reply(external_id, [{"answer_text": answer, "selected_options": []}])
        return {"answered": True, "result": result}

    def fetch_thread_history(self, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
        if not channel_id or not thread_ts:
            return []
        response = self.slack_client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=self.settings.thread_history_limit
        )
        messages = list(response.get("messages") or [])
        self._add_user_real_names(messages)
        return messages

    def _add_user_real_names(self, messages: list[dict[str, Any]]) -> None:
        user_ids = sorted({str(message.get("user")) for message in messages if message.get("user")})
        names: dict[str, str] = {}
        for user_id in user_ids:
            try:
                response = self.slack_client.users_info(user=user_id)
            except Exception:
                continue

            user = response.get("user") or {}
            profile = user.get("profile") or {}
            name = profile.get("real_name") or profile.get("display_name") or user.get("real_name") or user.get("name")
            if name:
                names[user_id] = str(name)

        for message in messages:
            user_id = str(message.get("user") or "")
            if user_id in names:
                message["user_real_name"] = names[user_id]

    def build_headers(self, event: dict[str, Any], *, thread_ts: str) -> dict:
        return {
            "source": "slack",
            "slack": {
                "team_id": event.get("team") or event.get("team_id"),
                "channel_id": event.get("channel"),
                "thread_ts": thread_ts,
                "message_ts": event.get("ts"),
                "user_id": event.get("user"),
                "user_name": event.get("username"),
                "permalink": event.get("permalink"),
            },
        }
