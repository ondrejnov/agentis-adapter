from __future__ import annotations

from typing import Any

from claude.activity_mapper import ClaudeActivityMapper


# ---------------------------------------------------------------------------
# ClaudeActivityMapper unit tests
# ---------------------------------------------------------------------------


def test_mapper_uses_real_claude_session_id_from_session_start():
    mapper = ClaudeActivityMapper(prompt="Popis ukolu")

    assert mapper.session_id == ""

    changed = mapper.consume(
        type("Event", (), {"type": "session_start", "data": {"session_id": "claude-session-123"}})()
    )

    assert changed is True
    assert mapper.session_id == "claude-session-123"
    snapshot = mapper.snapshot()
    assert snapshot[0]["info"]["sessionID"] == "claude-session-123"
    assert snapshot[0]["parts"][0]["sessionID"] == "claude-session-123"


def _event(event_type: str, data: dict[str, Any]) -> Any:
    return type("Event", (), {"type": event_type, "data": data})()


def _emit_tool_use(
    mapper: ClaudeActivityMapper, name: str, inp: dict[str, Any], call_id: str = "toolu_x"
) -> dict[str, Any]:
    mapper.consume(type("Event", (), {"type": "session_start", "data": {"session_id": "sid"}})())
    mapper.consume(
        type(
            "Event",
            (),
            {
                "type": "tool_use",
                "data": {"id": call_id, "name": name, "input": inp},
            },
        )()
    )
    snap = mapper.snapshot()
    for entry in snap:
        for part in entry.get("parts") or []:
            if part.get("type") == "tool" and part.get("callID") == call_id:
                return part
    raise AssertionError("tool part not found")


def test_mapper_tool_use_normalizes_read_input_and_title():
    mapper = ClaudeActivityMapper(prompt="x", cwd="/var/www/agentis")
    part = _emit_tool_use(mapper, "Read", {"file_path": "/var/www/agentis/frontend/app/foo.tsx"})

    state_input = part["state"]["input"]
    assert state_input["file_path"] == "/var/www/agentis/frontend/app/foo.tsx"
    # Frontend reads camelCase `filePath`.
    assert state_input["filePath"] == "/var/www/agentis/frontend/app/foo.tsx"
    # Title is the cwd-relative path so the run timeline shows what file was read.
    assert part["state"]["title"] == "frontend/app/foo.tsx"


def test_mapper_tool_use_bash_title_uses_description():
    mapper = ClaudeActivityMapper(prompt="x")
    part = _emit_tool_use(
        mapper,
        "Bash",
        {"command": "git log --oneline -20", "description": "Show recent commits"},
    )
    assert part["state"]["title"] == "Show recent commits"
    assert part["state"]["input"]["command"] == "git log --oneline -20"


def test_mapper_result_maps_cache_creation_to_cache_write():
    mapper = ClaudeActivityMapper(prompt="x")

    mapper.consume(type("Event", (), {"type": "session_start", "data": {"session_id": "sid"}})())
    mapper.consume(
        type(
            "Event",
            (),
            {
                "type": "result",
                "data": {
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 30,
                        "cache_creation_input_tokens": 40,
                    },
                    "cost_usd": 0.01,
                },
            },
        )()
    )

    assistant = mapper.snapshot()[-1]
    assert assistant["info"]["tokens"] == {
        "input": 10,
        "output": 20,
        "reasoning": 0,
        "cache": {"read": 30, "write": 40},
    }


def test_mapper_records_per_turn_tokens_from_assistant_messages():
    # Dva turny, každý s vlastním usage na assistant zprávě. Tokeny musí sednout
    # per-message a finální (kumulativní) result je už nesmí zopakovat.
    mapper = ClaudeActivityMapper(prompt="x")
    mapper.consume(_event("session_start", {"session_id": "sid"}))

    mapper.consume(_event("text", {"text": "First"}))
    mapper.consume(_event("assistant_message", {"message": {"usage": {"input_tokens": 10, "output_tokens": 4}}}))
    mapper.consume(_event("text", {"text": "Second"}))
    mapper.consume(_event("assistant_message", {"message": {"usage": {"input_tokens": 20, "output_tokens": 6}}}))
    mapper.consume(_event("result", {"usage": {"input_tokens": 20, "output_tokens": 6}, "cost_usd": 0.05}))

    assistant = [m for m in mapper.snapshot() if m["info"]["role"] == "assistant"]
    assert len(assistant) == 2
    assert assistant[0]["info"]["tokens"]["input"] == 10
    assert assistant[0]["info"]["tokens"]["output"] == 4
    assert assistant[1]["info"]["tokens"]["input"] == 20
    assert assistant[1]["info"]["tokens"]["output"] == 6
    assert sum(m["info"]["tokens"]["input"] for m in assistant) == 30


def test_mapper_dedupes_repeated_assistant_message_id_into_single_message():
    # Claude rozkládá jednu zprávu (stejné `message.id`) do více stream chunků
    # (thinking → tool_use) a na každém opakuje IDENTICKÝ usage. Nesmí se počítat
    # dvakrát: výsledkem je jedna zpráva s více parts a usage zapsaný jen jednou.
    mapper = ClaudeActivityMapper(prompt="x")
    mapper.consume(_event("session_start", {"session_id": "sid"}))

    mid = "msg_aaa"
    usage1 = {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 22040, "cache_creation_input_tokens": 9133}
    mapper.consume(_event("thinking", {"text": "", "message_id": mid}))
    mapper.consume(_event("assistant_message", {"message_id": mid, "message": {"id": mid, "usage": usage1}}))
    mapper.consume(_event("tool_use", {"id": "toolu_1", "name": "Bash", "input": {"command": "pwd"}, "message_id": mid}))
    mapper.consume(_event("assistant_message", {"message_id": mid, "message": {"id": mid, "usage": usage1}}))
    mapper.consume(_event("tool_result", {"tool_use_id": "toolu_1", "content": "/var/www/clarp", "is_error": False}))

    mid2 = "msg_bbb"
    usage2 = {"input_tokens": 8, "output_tokens": 2, "cache_read_input_tokens": 31173, "cache_creation_input_tokens": 172}
    mapper.consume(_event("text", {"text": "Hotovo", "message_id": mid2}))
    mapper.consume(_event("assistant_message", {"message_id": mid2, "message": {"id": mid2, "usage": usage2}}))
    # Finální (kumulativní) result tokeny už nesmí přidat — jen dovře poslední zprávu.
    mapper.consume(_event("result", {"usage": {"input_tokens": 18, "output_tokens": 5}, "cost_usd": 0.01}))

    assistants = [m for m in mapper.snapshot() if m["info"]["role"] == "assistant"]
    assert len(assistants) == 2

    a0 = assistants[0]
    # tool part patří do téže (první) zprávy, ne do nově založené
    assert any(p.get("type") == "tool" for p in a0["parts"])
    # právě jeden step-finish na zprávu
    assert sum(1 for p in a0["parts"] if p.get("type") == "step-finish") == 1
    assert a0["info"]["tokens"]["input"] == 10
    assert a0["info"]["tokens"]["output"] == 3
    assert a0["info"]["tokens"]["cache"] == {"read": 22040, "write": 9133}

    a1 = assistants[1]
    assert a1["info"]["tokens"]["input"] == 8
    assert sum(1 for p in a1["parts"] if p.get("type") == "step-finish") == 1

    # Součet per-message tokenů sedí na kumulativní result (18 = 10 + 8, 5 = 3 + 2).
    assert sum(m["info"]["tokens"]["input"] for m in assistants) == 18
    assert sum(m["info"]["tokens"]["output"] for m in assistants) == 5


def test_mapper_falls_back_to_result_tokens_without_per_turn_usage():
    # Bez assistant usage zůstává chování zpětně kompatibilní: tokeny z resultu.
    mapper = ClaudeActivityMapper(prompt="x")
    mapper.consume(_event("session_start", {"session_id": "sid"}))
    mapper.consume(_event("text", {"text": "Hi"}))
    mapper.consume(_event("result", {"usage": {"input_tokens": 7, "output_tokens": 3}, "cost_usd": 0.01}))

    assistant = [m for m in mapper.snapshot() if m["info"]["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["info"]["tokens"]["input"] == 7
    assert assistant[0]["info"]["tokens"]["output"] == 3


def test_mapper_tool_use_bash_title_falls_back_to_command_when_no_description():
    mapper = ClaudeActivityMapper(prompt="x")
    part = _emit_tool_use(mapper, "Bash", {"command": "ls -la"})
    assert part["state"]["title"] == "ls -la"


def test_mapper_tool_use_grep_title_is_pattern():
    mapper = ClaudeActivityMapper(prompt="x")
    part = _emit_tool_use(mapper, "Grep", {"pattern": "useApiQuery", "path": "/var/www/agentis/frontend"})
    assert part["state"]["title"] == "useApiQuery"


def test_mapper_tool_use_task_aliases_subagent_type_to_camel_case():
    mapper = ClaudeActivityMapper(prompt="x")
    part = _emit_tool_use(
        mapper,
        "Task",
        {"description": "Audit branch", "subagent_type": "general-purpose", "prompt": "..."},
    )
    state_input = part["state"]["input"]
    assert state_input["subagent_type"] == "general-purpose"
    assert state_input["subagentType"] == "general-purpose"
    assert part["state"]["title"] == "Audit branch"


def test_mapper_tool_use_todowrite_title_counts_todos():
    mapper = ClaudeActivityMapper(prompt="x")
    part = _emit_tool_use(
        mapper,
        "TodoWrite",
        {"todos": [{"content": "a", "status": "pending"}, {"content": "b", "status": "completed"}]},
    )
    assert part["state"]["title"] == "2 todos"


def test_mapper_tool_use_unknown_tool_keeps_input_and_uses_name_as_title():
    mapper = ClaudeActivityMapper(prompt="x")
    part = _emit_tool_use(mapper, "MysteryTool", {"foo": "bar"})
    assert part["state"]["input"] == {"foo": "bar"}
    assert part["state"]["title"] == "MysteryTool"
