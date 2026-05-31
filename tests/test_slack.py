from __future__ import annotations

from typing import Any

from slack.config import DEFAULT_SLACK_TASK_LABEL_ID, SlackSettings
from slack.guards import EventDeduper, GlobalRateLimiter, should_ignore_event
from slack.listener import SlackMentionService
from slack.text import (
    normalize_slack_text,
    parse_question_answer,
    plaintext_to_lexical,
    slack_history_to_context,
)


def make_settings(**overrides: Any) -> SlackSettings:
    values: dict[str, Any] = {
        "slack_bot_token": "xoxb",
        "slack_app_token": "xapp",
        "default_project": "project-1",
        "default_agent": "build",
        "default_model": "claude-haiku-4-5-20251001",
        "default_effort": "high",
        "default_adapter": "claude",
        "default_adapter_engine": None,
        "default_environment": None,
        "task_label_id": DEFAULT_SLACK_TASK_LABEL_ID,
        "rate_limit_window_seconds": 60,
        "rate_limit_max_events": 10,
        "thread_history_limit": 200,
    }
    values.update(overrides)
    return SlackSettings(**values)


class FakeAgentis:
    def __init__(self, external: list[Any] | None = None) -> None:
        self.external = list(external or [])
        self.calls: list[tuple[str, Any]] = []

    def find_by_external_ref(self, filters: dict[str, str]) -> dict | None:
        self.calls.append(("find", filters))
        return self.external.pop(0) if self.external else None

    def save_task(self, data: dict[str, Any]) -> dict:
        self.calls.append(("save_task", data))
        return {"form": {"id": "task-1", "title": data["title"], "headers": data["headers"]}}

    def start_run(self, task_id: str, *, start_adapter: bool = True) -> dict:
        self.calls.append(("start_run", {"task_id": task_id, "start_adapter": start_adapter}))
        return {"item": {"id": "run-1"}}

    def question_reply(self, external_id: str, results: list[dict]) -> dict:
        self.calls.append(("question_reply", {"external_id": external_id, "results": results}))
        return {"ok": True}


class FakeSlack:
    def __init__(self) -> None:
        self.reactions: list[dict[str, Any]] = []
        self.user_names = {"U1": "Alice Example"}

    def conversations_replies(self, channel: str, ts: str, limit: int = 200) -> dict[str, Any]:
        return {"messages": [{"user": "U1", "text": "<@UBOT> build it", "ts": ts}]}

    def users_info(self, user: str) -> dict[str, Any]:
        return {"user": {"id": user, "profile": {"real_name": self.user_names[user]}}}

    def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        self.reactions.append(kwargs)
        return {"ok": True}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def test_normalize_slack_text_strips_mention_and_unwraps_links():
    text = normalize_slack_text("<@UBOT>  please see <https://x.dev|the docs> now", bot_user_id="UBOT")
    assert text == "please see the docs (https://x.dev) now"


def test_slack_history_to_context_uses_real_names():
    context = slack_history_to_context(
        [
            {"user_real_name": "Alice Example", "text": "<@UBOT> build it"},
            {"text": ""},
        ]
    )
    assert context == "[Alice Example] build it"


def test_plaintext_to_lexical_wraps_text():
    doc = plaintext_to_lexical("hello")
    assert doc["root"]["children"][0]["children"][0]["text"] == "hello"


def test_parse_question_answer():
    assert parse_question_answer("answer q1: Ship it") == ("q1", "Ship it")
    assert parse_question_answer("just chatting") is None


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_event_deduper_blocks_second_occurrence():
    deduper = EventDeduper()
    assert deduper.seen_before("evt-1") is False
    assert deduper.seen_before("evt-1") is True
    assert deduper.seen_before("evt-2") is False


def test_rate_limiter_blocks_over_limit():
    limiter = GlobalRateLimiter(max_events=2, window_seconds=60)
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False


def test_should_ignore_event_for_bots_and_self():
    assert should_ignore_event({"bot_id": "B1"}) is True
    assert should_ignore_event({"subtype": "message_changed"}) is True
    assert should_ignore_event({"user": "UBOT"}, bot_user_id="UBOT") is True
    assert should_ignore_event({"user": "U1"}, bot_user_id="UBOT") is False


# ---------------------------------------------------------------------------
# SlackMentionService
# ---------------------------------------------------------------------------


def _service(agentis: FakeAgentis, slack: FakeSlack) -> SlackMentionService:
    return SlackMentionService(settings=make_settings(), agentis=agentis, slack_client=slack, bot_user_id="UBOT")


def test_new_mention_creates_task_and_starts_run():
    agentis = FakeAgentis()
    slack = FakeSlack()
    service = _service(agentis, slack)

    result = service.handle_app_mention(
        {"team": "T1", "channel": "C1", "ts": "1.1", "user": "U1", "text": "<@UBOT> build it"}
    )

    assert result["created"] is True
    assert [call[0] for call in agentis.calls] == ["find", "save_task", "start_run"]
    save_task = next(call for call in agentis.calls if call[0] == "save_task")
    assert save_task[1]["labels"] == [DEFAULT_SLACK_TASK_LABEL_ID]
    assert save_task[1]["adapter"] == "claude"
    assert save_task[1]["headers"]["source"] == "slack"
    assert slack.reactions == [{"channel": "C1", "timestamp": "1.1", "name": "eyes"}]


def test_new_mention_embeds_real_user_names_in_description():
    agentis = FakeAgentis()
    service = _service(agentis, FakeSlack())

    service.handle_app_mention({"team": "T1", "channel": "C1", "ts": "1.1", "user": "U1", "text": "<@UBOT> build it"})

    save_task = next(call for call in agentis.calls if call[0] == "save_task")
    description = save_task[1]["description"]["root"]["children"][0]["children"][0]["text"]
    assert "[Alice Example] build it" in description


def test_duplicate_task_lookup_skips_create():
    agentis = FakeAgentis(external=[{"id": "task-1"}])
    service = _service(agentis, FakeSlack())

    result = service.handle_app_mention(
        {"team": "T1", "channel": "C1", "ts": "1.1", "user": "U1", "text": "<@UBOT> build"}
    )

    assert result == {"ignored": True, "reason": "duplicate_task", "task": {"id": "task-1"}}
    assert [call[0] for call in agentis.calls] == ["find"]


def test_bot_event_is_ignored():
    service = _service(FakeAgentis(), FakeSlack())
    assert service.handle_app_mention({"bot_id": "B1"}) == {"ignored": True, "reason": "bot"}


def test_duplicate_event_id_is_ignored():
    service = _service(FakeAgentis(), FakeSlack())
    event = {"team": "T1", "channel": "C1", "ts": "1.1", "user": "U1", "text": "<@UBOT> hi"}

    first = service.handle_app_mention(event, event_id="evt-1")
    second = service.handle_app_mention(event, event_id="evt-1")

    assert first["created"] is True
    assert second == {"ignored": True, "reason": "duplicate"}


def test_rate_limited_mention_is_ignored():
    service = SlackMentionService(
        settings=make_settings(rate_limit_max_events=1),
        agentis=FakeAgentis(),
        slack_client=FakeSlack(),
        bot_user_id="UBOT",
    )

    first = service.handle_app_mention(
        {"team": "T1", "channel": "C1", "ts": "1.1", "user": "U1", "text": "<@UBOT> one"}, event_id="a"
    )
    second = service.handle_app_mention(
        {"team": "T1", "channel": "C1", "ts": "1.2", "user": "U1", "text": "<@UBOT> two"}, event_id="b"
    )

    assert first["created"] is True
    assert second == {"ignored": True, "reason": "rate_limited"}


def test_question_answer_forwards_to_agentis():
    agentis = FakeAgentis()
    service = _service(agentis, FakeSlack())

    result = service.handle_message({"user": "U1", "text": "answer q1: Ship it"})

    assert result["answered"] is True
    assert agentis.calls == [
        ("question_reply", {"external_id": "q1", "results": [{"answer_text": "Ship it", "selected_options": []}]})
    ]


def test_plain_message_without_answer_is_ignored():
    agentis = FakeAgentis()
    service = _service(agentis, FakeSlack())

    result = service.handle_message({"user": "U1", "text": "hello there"})

    assert result == {"ignored": True, "reason": "not_question_answer"}
    assert agentis.calls == []
