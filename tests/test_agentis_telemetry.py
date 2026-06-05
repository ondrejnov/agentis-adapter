from __future__ import annotations

from typing import Any

import pytest

from common.agentis_telemetry import AgentisTelemetry, _unified_to_native
from common.agentiscode import AgentEvent


class FakeClient:
    """In-memory AgentisJsonRpcClient náhrada — zaznamenává volání a vrací nakonfigurované výsledky."""

    def __init__(self, results: dict[str, Any] | None = None, *, fail_methods: set[str] | None = None) -> None:
        self.results = results or {}
        self.fail_methods = fail_methods or set()
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def call(self, *, method: str, params: dict[str, Any], request_id: Any | None = None) -> Any:
        self.calls.append({"method": method, "params": params})
        if method in self.fail_methods:
            from common.agentis import AgentisJsonRpcError

            raise AgentisJsonRpcError(f"{method} boom")
        return self.results.get(method, {"ok": True})

    def close(self) -> None:
        self.closed = True

    def methods(self) -> list[str]:
        return [c["method"] for c in self.calls]

    def params_for(self, method: str) -> dict[str, Any]:
        return next(c["params"] for c in self.calls if c["method"] == method)


def _stream() -> list[AgentEvent]:
    return [
        AgentEvent("session", {"adapter": "claude", "session_id": "ses_1", "model": "claude-x", "cwd": "/w"}),
        AgentEvent("text", {"text": "Hello"}),
        AgentEvent("tool", {"id": "t1", "name": "Read", "status": "running", "input": {"file_path": "/w/a.py"}}),
        AgentEvent("tool", {"id": "t1", "status": "completed", "output": "data"}),
        AgentEvent(
            "result", {"session_id": "ses_1", "usage": {"input_tokens": 3}, "cost_usd": 0.02, "is_error": False}
        ),
    ]


def test_unified_to_native_maps_event_types() -> None:
    assert _unified_to_native(AgentEvent("session", {"session_id": "s"})).type == "session_start"
    assert _unified_to_native(AgentEvent("text", {"text": "x"})).type == "text"
    assert _unified_to_native(AgentEvent("reasoning", {"text": "y"})).type == "thinking"
    running = _unified_to_native(AgentEvent("tool", {"id": "t", "name": "Read", "status": "running"}))
    assert running.type == "tool_use" and running.data["id"] == "t"
    completed = _unified_to_native(AgentEvent("tool", {"id": "t", "status": "completed", "output": "ok"}))
    assert completed.type == "tool_result" and completed.data == {
        "tool_use_id": "t",
        "content": "ok",
        "is_error": False,
    }
    errored = _unified_to_native(AgentEvent("tool", {"id": "t", "status": "error", "error": "bad"}))
    assert errored.data == {"tool_use_id": "t", "content": "bad", "is_error": True}
    assert _unified_to_native(AgentEvent("result", {"is_error": False})).type == "result"
    # error / stderr do transcriptu nepatří
    assert _unified_to_native(AgentEvent("error", {"message": "x"})) is None
    assert _unified_to_native(AgentEvent("stderr", {"line": "x"})) is None


def test_telemetry_full_run_creates_run_binds_session_and_pushes_logs() -> None:
    client = FakeClient(results={"task.start_run": {"item": {"id": "run-9"}}})
    telemetry = AgentisTelemetry(task_id="task-1", prompt="udelej X", adapter="claude", client=client)

    run_id = telemetry.start()
    assert run_id == "run-9"

    for event in _stream():
        telemetry.handle(event)
    telemetry.finish()

    methods = client.methods()
    # run založen, hned za ním adapter_event started
    assert methods[0] == "task.start_run"
    assert methods[1] == "run.adapter_event"
    # session binding proběhne při session eventu, před prvním store_activity_log
    assert "run.store_session_id" in methods
    assert methods.index("run.store_session_id") < methods.index("session.store_activity_log")
    assert client.params_for("run.store_session_id") == {"run_id": "run-9", "session_id": "ses_1"}

    adapter_events = [c["params"] for c in client.calls if c["method"] == "run.adapter_event"]
    started = adapter_events[0]
    assert started["status"] == "started" and started["kind"] == "claude_run"
    # started krok se uzavře STEJNÝM event_id → spinner se přepne na hotovo
    run_step_finish = adapter_events[1]
    assert run_step_finish["kind"] == "claude_run" and run_step_finish["status"] == "success"
    assert run_step_finish["event_id"] == started["event_id"]
    # koncový idle event uzavře adapter_state a vyšle run.finished
    idle = adapter_events[2]
    assert idle["kind"] == "idle" and idle["status"] == "success"

    # finální odpověď se bez explicitního opt-in neposílá jako task komentář
    assert "task.add_agent_comment" not in methods

    # uložená aktivita nese prompt i text agenta ve správném tvaru
    last_log = [c for c in client.calls if c["method"] == "session.store_activity_log"][-1]["params"]
    assert last_log["session_id"] == "ses_1"
    roles = [m["info"]["role"] for m in last_log["messages"]]
    assert roles[0] == "user" and "assistant" in roles


def test_telemetry_records_per_turn_tokens_across_messages() -> None:
    # Dva turny, každý s vlastním `step` usage. Tokeny musí sednout per-message,
    # ať jdou sčítat — finální `result` je už nesmí zopakovat.
    client = FakeClient(results={"task.start_run": {"item": {"id": "run-1"}}})
    telemetry = AgentisTelemetry(task_id="task-1", prompt="udelej X", adapter="claude", client=client)
    telemetry.start()

    telemetry.handle(AgentEvent("session", {"adapter": "claude", "session_id": "ses_1"}))
    telemetry.handle(AgentEvent("text", {"text": "First"}))
    telemetry.handle(AgentEvent("step", {"usage": {"input_tokens": 10, "output_tokens": 4}, "cost_usd": 0.01}))
    telemetry.handle(AgentEvent("text", {"text": "Second"}))
    telemetry.handle(AgentEvent("step", {"usage": {"input_tokens": 20, "output_tokens": 6}, "cost_usd": 0.02}))
    telemetry.handle(
        AgentEvent("result", {"session_id": "ses_1", "usage": {"input_tokens": 20}, "cost_usd": 0.02, "is_error": False})
    )
    telemetry.finish()

    messages = [c for c in client.calls if c["method"] == "session.store_activity_log"][-1]["params"]["messages"]
    assistant = [m for m in messages if m["info"]["role"] == "assistant"]
    # Každý turn = vlastní assistant zpráva s vlastními tokeny (žádná navíc z result).
    assert len(assistant) == 2
    assert assistant[0]["info"]["tokens"]["input"] == 10
    assert assistant[0]["info"]["tokens"]["output"] == 4
    assert assistant[1]["info"]["tokens"]["input"] == 20
    assert assistant[1]["info"]["tokens"]["output"] == 6
    # Součet napříč turny dává reálnou spotřebu, ne jen poslední kontext.
    assert sum(m["info"]["tokens"]["input"] for m in assistant) == 30


def test_telemetry_uses_existing_run_id_without_starting_new_run() -> None:
    client = FakeClient()
    telemetry = AgentisTelemetry(
        task_id="task-1", prompt="udelej X", adapter="claude", run_id="run-existing", client=client
    )

    run_id = telemetry.start()

    assert run_id == "run-existing"
    assert client.methods() == ["run.adapter_event"]
    assert client.calls[0]["params"]["run_id"] == "run-existing"


def test_telemetry_final_comment_can_set_task_status() -> None:
    client = FakeClient()
    telemetry = AgentisTelemetry(
        task_id="task-1",
        prompt="udelej X",
        adapter="claude",
        run_id="run-existing",
        task_status=4,
        last_message_to_comment=True,
        client=client,
    )

    telemetry.start()
    for event in _stream():
        telemetry.handle(event)
    telemetry.finish()

    assert client.params_for("task.add_agent_comment") == {
        "run_id": "run-existing",
        "body": "Hello",
        "comment_type": "primary",
        "status": 4,
    }


def test_telemetry_final_comment_uses_only_last_text_message() -> None:
    client = FakeClient()
    telemetry = AgentisTelemetry(
        task_id="task-1",
        prompt="udelej X",
        adapter="claude",
        run_id="run-existing",
        last_message_to_comment=True,
        client=client,
    )

    telemetry.start()
    telemetry.handle(AgentEvent("session", {"session_id": "ses_1"}))
    telemetry.handle(AgentEvent("text", {"text": "Starsi odpoved."}))
    telemetry.handle(AgentEvent("reasoning", {"text": "premyslim"}))
    telemetry.handle(AgentEvent("text", {"text": "Final"}))
    telemetry.handle(AgentEvent("text", {"text": "ni odpoved."}))
    telemetry.finish()

    assert client.params_for("task.add_agent_comment")["body"] == "Finalni odpoved."


def test_telemetry_marks_failed_run_on_error_result() -> None:
    client = FakeClient(results={"task.start_run": {"item": {"id": "run-err"}}})
    telemetry = AgentisTelemetry(task_id="task-1", prompt="x", adapter="opencode", client=client)
    telemetry.start()
    telemetry.handle(AgentEvent("session", {"session_id": "ses_2"}))
    telemetry.handle(AgentEvent("result", {"is_error": True}))
    telemetry.finish()

    finish = next(c["params"] for c in reversed(client.calls) if c["method"] == "run.adapter_event")
    assert finish["status"] == "failed"


def test_telemetry_disables_itself_when_run_id_missing() -> None:
    client = FakeClient(results={"task.start_run": {"item": {}}})
    errors: list[str] = []
    telemetry = AgentisTelemetry(task_id="task-1", prompt="x", adapter="claude", client=client, on_error=errors.append)

    assert telemetry.start() is None
    assert telemetry.active is False
    # handle/finish jsou no-op, žádné další RPC se neposílá
    for event in _stream():
        telemetry.handle(event)
    telemetry.finish()
    assert client.methods() == ["task.start_run"]
    assert errors  # ohlásilo, že je telemetrie vypnutá


def test_telemetry_swallows_rpc_errors() -> None:
    client = FakeClient(
        results={"task.start_run": {"item": {"id": "run-1"}}},
        fail_methods={"session.store_activity_log"},
    )
    errors: list[str] = []
    telemetry = AgentisTelemetry(task_id="task-1", prompt="x", adapter="claude", client=client, on_error=errors.append)
    telemetry.start()
    # nesmí vyhodit výjimku, jen ohlásit přes on_error
    for event in _stream():
        telemetry.handle(event)
    telemetry.finish()
    assert any("session.store_activity_log" in message for message in errors)


def test_telemetry_requires_task_id_and_endpoint() -> None:
    with pytest.raises(ValueError, match="task_id"):
        AgentisTelemetry(task_id=" ", prompt="x", adapter="claude", endpoint="http://x")
    with pytest.raises(ValueError, match="endpoint"):
        AgentisTelemetry(task_id="task-1", prompt="x", adapter="claude")
