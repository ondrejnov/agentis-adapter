from __future__ import annotations

from typing import Any

import pytest

from common.agentis_telemetry import AgentisTelemetry, _unified_to_native
from agentiscode import AgentEvent


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
    assert client.params_for("run.store_session_id") == {"run_id": "run-9", "session_id": "ses_1", "primary": True}

    adapter_events = [c["params"] for c in client.calls if c["method"] == "run.adapter_event"]
    # běh spuštěn — agentiscode krok se posílá rovnou jako success (bez started spinneru)
    started = adapter_events[0]
    assert started["status"] == "success" and started["kind"] == "agentiscode"
    # koncový idle event uzavře adapter_state a vyšle run.finished
    idle = adapter_events[1]
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
        AgentEvent(
            "result", {"session_id": "ses_1", "usage": {"input_tokens": 20}, "cost_usd": 0.02, "is_error": False}
        )
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


def test_telemetry_does_not_finish_existing_run_id() -> None:
    client = FakeClient()
    telemetry = AgentisTelemetry(
        task_id="task-1", prompt="udelej X", adapter="claude", run_id="run-existing", client=client
    )

    telemetry.start()
    for event in _stream():
        telemetry.handle(event)
    telemetry.finish()

    adapter_events = [c["params"] for c in client.calls if c["method"] == "run.adapter_event"]
    assert [event["kind"] for event in adapter_events] == ["agentiscode"]


def test_telemetry_can_bind_secondary_session() -> None:
    client = FakeClient()
    telemetry = AgentisTelemetry(
        task_id="task-1",
        prompt="udelej X",
        adapter="claude",
        run_id="run-existing",
        primary_session=False,
        client=client,
    )

    telemetry.start()
    telemetry.handle(AgentEvent("session", {"session_id": "ses-secondary"}))

    assert client.params_for("run.store_session_id") == {
        "run_id": "run-existing",
        "session_id": "ses-secondary",
        "primary": False,
    }


def test_telemetry_stores_subagent_messages_under_its_own_session() -> None:
    client = FakeClient()
    telemetry = AgentisTelemetry(
        task_id="task-1", prompt="Delegate work", adapter="opencode", run_id="run-existing", client=client
    )
    telemetry.start()

    events = [
        AgentEvent("session", {"session_id": "ses-main"}),
        AgentEvent("text", {"text": "Delegating", "session_id": "ses-main"}),
        AgentEvent("session", {"session_id": "ses-child"}),
        AgentEvent("text", {"text": "Subtask result", "session_id": "ses-child"}),
        AgentEvent(
            "step",
            {"usage": {"input_tokens": 2, "output_tokens": 1}, "cost_usd": 0.01, "session_id": "ses-child"},
        ),
        AgentEvent("session", {"session_id": "ses-main"}),
        AgentEvent("text", {"text": "Done", "session_id": "ses-main"}),
    ]
    for event in events:
        telemetry.handle(event)
    telemetry.finish()

    bindings = [call["params"] for call in client.calls if call["method"] == "run.store_session_id"]
    assert bindings == [
        {"run_id": "run-existing", "session_id": "ses-main", "primary": True},
        {"run_id": "run-existing", "session_id": "ses-child", "primary": False},
    ]

    logs = [call["params"] for call in client.calls if call["method"] == "session.store_activity_log"]
    child_log = next(log for log in reversed(logs) if log["session_id"] == "ses-child")
    main_log = next(log for log in reversed(logs) if log["session_id"] == "ses-main")

    assert any(part.get("text") == "Subtask result" for message in child_log["messages"] for part in message["parts"])
    assert not any(
        part.get("text") == "Subtask result" for message in main_log["messages"] for part in message["parts"]
    )
    for log in (main_log, child_log):
        assert all(message["info"]["sessionID"] == log["session_id"] for message in log["messages"])
        assert all(part["sessionID"] == log["session_id"] for message in log["messages"] for part in message["parts"])


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


def test_telemetry_retries_completed_snapshot_after_transient_store_failure() -> None:
    class FlakyStoreClient(FakeClient):
        failed_completed_snapshot = False

        def call(self, *, method: str, params: dict[str, Any], request_id: Any | None = None) -> Any:
            self.calls.append({"method": method, "params": params})
            has_completed_tool = any(
                part.get("state", {}).get("status") == "completed"
                for message in params.get("messages", [])
                for part in message.get("parts", [])
                if isinstance(part.get("state"), dict)
            )
            if method == "session.store_activity_log" and has_completed_tool and not self.failed_completed_snapshot:
                self.failed_completed_snapshot = True
                from common.agentis import AgentisJsonRpcError

                raise AgentisJsonRpcError("temporary failure")
            return self.results.get(method, {"ok": True})

    client = FlakyStoreClient()
    telemetry = AgentisTelemetry(task_id="task-1", prompt="x", adapter="opencode", run_id="run-existing", client=client)
    telemetry.start()
    telemetry.handle(AgentEvent("session", {"session_id": "ses_1"}))
    telemetry.handle(
        AgentEvent("tool", {"id": "t1", "name": "bash", "status": "running", "input": {"command": "true"}})
    )
    telemetry.handle(AgentEvent("tool", {"id": "t1", "status": "completed", "output": "ok"}))
    telemetry.finish()

    completed_calls = [
        call
        for call in client.calls
        if call["method"] == "session.store_activity_log"
        and any(
            part.get("state", {}).get("status") == "completed"
            for message in call["params"]["messages"]
            for part in message["parts"]
            if isinstance(part.get("state"), dict)
        )
    ]
    assert len(completed_calls) == 2


def test_telemetry_stores_completed_tool_without_running_event() -> None:
    client = FakeClient()
    telemetry = AgentisTelemetry(task_id="task-1", prompt="x", adapter="opencode", run_id="run-existing", client=client)
    telemetry.start()
    telemetry.handle(AgentEvent("session", {"session_id": "ses_1"}))
    telemetry.handle(
        AgentEvent(
            "tool",
            {
                "id": "t1",
                "name": "bash",
                "status": "completed",
                "input": {"command": "true"},
                "output": "ok",
            },
        )
    )

    messages = [call for call in client.calls if call["method"] == "session.store_activity_log"][-1]["params"][
        "messages"
    ]
    tool = next(part for message in messages for part in message["parts"] if part.get("callID") == "t1")
    assert tool["tool"] == "bash"
    assert tool["state"]["status"] == "completed"
    assert tool["state"]["output"] == "ok"


def test_telemetry_retries_failed_session_binding_on_next_event() -> None:
    class FlakyBindingClient(FakeClient):
        binding_attempts = 0

        def call(self, *, method: str, params: dict[str, Any], request_id: Any | None = None) -> Any:
            self.calls.append({"method": method, "params": params})
            if method == "run.store_session_id":
                self.binding_attempts += 1
                if self.binding_attempts == 1:
                    from common.agentis import AgentisJsonRpcError

                    raise AgentisJsonRpcError("temporary failure")
            return self.results.get(method, {"ok": True})

    client = FlakyBindingClient()
    telemetry = AgentisTelemetry(task_id="task-1", prompt="x", adapter="opencode", run_id="run-existing", client=client)
    telemetry.start()
    telemetry.handle(AgentEvent("session", {"session_id": "ses_1"}))
    telemetry.handle(AgentEvent("text", {"session_id": "ses_1", "text": "done"}))

    assert client.binding_attempts == 2
    assert "session.store_activity_log" in client.methods()


def test_telemetry_treats_ok_false_as_failed_store() -> None:
    client = FakeClient(results={"session.store_activity_log": {"ok": False, "error": "run not found"}})
    errors: list[str] = []
    telemetry = AgentisTelemetry(
        task_id="task-1",
        prompt="x",
        adapter="opencode",
        run_id="run-existing",
        client=client,
        on_error=errors.append,
    )
    telemetry.start()
    telemetry.handle(AgentEvent("session", {"session_id": "ses_1"}))
    attempts_before_finish = client.methods().count("session.store_activity_log")
    telemetry.finish()

    assert client.methods().count("session.store_activity_log") == attempts_before_finish + 1
    assert any("run not found" in message for message in errors)


def test_telemetry_requires_task_id_and_endpoint() -> None:
    with pytest.raises(ValueError, match="task_id"):
        AgentisTelemetry(task_id=" ", prompt="x", adapter="claude", endpoint="http://x")
    with pytest.raises(ValueError, match="endpoint"):
        AgentisTelemetry(task_id="task-1", prompt="x", adapter="claude")
