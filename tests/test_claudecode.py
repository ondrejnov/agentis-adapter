from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from claude.api import create_app
from common.config import Settings
from common.models import (
    AdapterOptionsPayload,
    AgentExecutionContextPayload,
)
from claude.adapter import ClaudeCodeAdapterService
from common.kubernetes_runtime import KubernetesAdapterService
from claude.activity_mapper import ClaudeActivityMapper
from claude.session_manager import ClaudeSessionManager, _ClaudeSession
from claude.client import KubectlExecTarget
from common.integrations.github_pr import GithubPrResult


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8002,
        "default_namespace": "agentis",
        "app_host": None,
        "manifest_path": Path("/tmp/opencode.yaml"),
        "worktree_root": Path("/var/www/worktrees"),
        "public_base_url": "http://adapter.internal:8002",
        "agentis_endpoint": None,
        "agentis_token": None,
        "claude_run_mode": "local",
        "claude_pod_selector": "deployment/opencode",
        "claude_pod_container": "opencode",
        "kubectl_command": "kubectl",
    }
    values.update(overrides)
    return Settings(**values)


def make_context(**overrides: Any) -> AgentExecutionContextPayload:
    payload: dict[str, Any] = {
        "run_id": "run-1",
        "task_id": "task-1",
        "title": "Implementace nove funkce",
        "description": "Popis ukolu",
        "project_slug": "agentis",
        "working_dir": "/var/www/repo",
        "adapter": AdapterOptionsPayload(agent="build", model="claude-haiku-4-5-20251001"),
    }
    payload.update(overrides)
    return AgentExecutionContextPayload(**payload)


# ---------------------------------------------------------------------------
# ClaudeCodeAdapterService unit tests
# ---------------------------------------------------------------------------


def test_deploy_is_skipped_for_local_claude():
    adapter = ClaudeCodeAdapterService(
        context=make_context(),
        settings=make_settings(),
        session_manager=MagicMock(spec=ClaudeSessionManager),
    )

    result = adapter.deploy()

    assert result == {
        "action": "deploy",
        "task_id": "task-1",
        "status": "skipped",
        "reason": "claude_local",
    }


def test_wait_ready_returns_local_url():
    adapter = ClaudeCodeAdapterService(
        context=make_context(),
        settings=make_settings(),
        session_manager=MagicMock(spec=ClaudeSessionManager),
    )

    result = adapter.wait_ready()

    assert result == {
        "action": "wait_ready",
        "task_id": "task-1",
        "url": "local://claude",
        "status": "skipped",
    }


def test_start_session_starts_session_manager_and_persists_session_id(monkeypatch):
    manager = MagicMock(spec=ClaudeSessionManager)
    manager.start.return_value = "ses_abc123"
    persisted: list[str] = []

    monkeypatch.setattr(
        ClaudeCodeAdapterService,
        "_persist_agentis_session_id",
        lambda self, session_id: persisted.append(session_id),
    )

    context = make_context()
    adapter = ClaudeCodeAdapterService(
        context=context,
        settings=make_settings(worktree_root=Path("/srv/worktrees")),
        session_manager=manager,
    )

    result = adapter.start_session(pod_url="local://claude")

    assert result == {
        "action": "start_session",
        "task_id": "task-1",
        "session_id": "ses_abc123",
    }
    manager.start.assert_called_once()
    kwargs = manager.start.call_args.kwargs
    assert kwargs["context"] is context
    assert kwargs["worktree"] == "/srv/worktrees/task-1"
    assert kwargs["prompt"] == "Popis ukolu"
    assert context.session_id == "ses_abc123"
    assert persisted == ["ses_abc123"]


def test_start_session_falls_back_to_title_when_description_missing(monkeypatch):
    manager = MagicMock(spec=ClaudeSessionManager)
    manager.start.return_value = "ses_xyz"
    monkeypatch.setattr(ClaudeCodeAdapterService, "_persist_agentis_session_id", lambda self, session_id: None)

    context = make_context(description="")
    adapter = ClaudeCodeAdapterService(
        context=context,
        settings=make_settings(),
        session_manager=manager,
    )

    adapter.start_session()

    assert manager.start.call_args.kwargs["prompt"] == "Implementace nove funkce"


def test_add_message_requires_session_id():
    adapter = ClaudeCodeAdapterService(
        context=make_context(),
        settings=make_settings(),
        session_manager=MagicMock(spec=ClaudeSessionManager),
    )

    with pytest.raises(RuntimeError, match="session_id"):
        adapter.add_message("ahoj")


def test_add_message_forwards_to_session_manager():
    manager = MagicMock(spec=ClaudeSessionManager)
    context = make_context(session_id="ses_abc")
    adapter = ClaudeCodeAdapterService(
        context=context,
        settings=make_settings(worktree_root=Path("/srv/worktrees")),
        session_manager=manager,
    )

    result = adapter.add_message("ahoj")

    assert result == {
        "action": "add_message",
        "task_id": "task-1",
        "session_id": "ses_abc",
    }
    manager.send.assert_called_once_with(
        session_id="ses_abc",
        context=context,
        worktree="/srv/worktrees/task-1",
        prompt="ahoj",
    )


def test_abort_delegates_to_session_manager():
    manager = MagicMock(spec=ClaudeSessionManager)
    adapter = ClaudeCodeAdapterService(
        context=make_context(),
        settings=make_settings(),
        session_manager=manager,
    )

    result = adapter.abort("ses_abc")

    assert result == {"action": "abort", "task_id": "task-1", "session_id": "ses_abc"}
    manager.abort.assert_called_once_with("ses_abc")


def test_start_session_passes_kubectl_target_in_kubernetes_mode(monkeypatch):
    manager = MagicMock(spec=ClaudeSessionManager)
    manager.start.return_value = "ses_k8s"
    monkeypatch.setattr(ClaudeCodeAdapterService, "_persist_agentis_session_id", lambda self, session_id: None)

    context = make_context(namespace="task-7-demo")
    adapter = ClaudeCodeAdapterService(
        context=context,
        settings=make_settings(claude_run_mode="kubernetes"),
        session_manager=manager,
    )

    adapter.start_session()

    target = manager.start.call_args.kwargs["kubectl_target"]
    assert isinstance(target, KubectlExecTarget)
    assert target.namespace == "task-7-demo"
    assert target.selector == "deployment/opencode"
    assert target.container == "opencode"


def test_context_runtime_local_overrides_kubernetes_mode(monkeypatch):
    manager = MagicMock(spec=ClaudeSessionManager)
    manager.start.return_value = "ses_local"
    monkeypatch.setattr(ClaudeCodeAdapterService, "_persist_agentis_session_id", lambda self, session_id: None)

    context = make_context(adapter=AdapterOptionsPayload(runtime="local", agent="build"))
    adapter = ClaudeCodeAdapterService(
        context=context,
        settings=make_settings(claude_run_mode="kubernetes"),
        session_manager=manager,
    )

    deploy_result = adapter.deploy()
    adapter.start_session()

    assert deploy_result["status"] == "skipped"
    assert "kubectl_target" not in manager.start.call_args.kwargs


def test_session_manager_start_snapshots_sources(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, str, str]] = []
    manager = ClaudeSessionManager(settings=make_settings())
    context = make_context()
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def fake_snapshot(worktree_arg: str, snapshot_key: str, label: str) -> None:
        calls.append((worktree_arg, snapshot_key, label))

    def fake_spawn(sess: _ClaudeSession, **_: Any) -> None:
        manager._bind_session_id(sess, "sess-1")

    monkeypatch.setattr("claude.session_manager.snapshot_sources_best_effort", fake_snapshot)
    monkeypatch.setattr(manager, "_spawn_thread", fake_spawn)

    session_id = manager.start(context=context, worktree=str(worktree), prompt="Popis ukolu")

    assert session_id == "sess-1"
    assert calls[0][0] == str(worktree)
    assert calls[0][2] == "claude-start"
    assert calls[0][1].startswith("claude-run-1-task-1-claude-pending-")


def test_session_manager_send_snapshots_feedback(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, str, str]] = []
    manager = ClaudeSessionManager(settings=make_settings())
    context = make_context(session_id="sess-1")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manager._sessions["sess-1"] = _ClaudeSession(
        session_id="sess-1",
        pending_key="pending",
        context=context,
        worktree=str(worktree),
        claude_session_id="sess-1",
    )

    monkeypatch.setattr(
        "claude.session_manager.snapshot_sources_best_effort",
        lambda worktree_arg, snapshot_key, label: calls.append((worktree_arg, snapshot_key, label)),
    )
    monkeypatch.setattr(manager, "_spawn_thread", lambda *args, **kwargs: None)
    monkeypatch.setattr("claude.session_manager.uuid4", lambda: type("Uuid", (), {"hex": "abc123"})())

    manager.send(session_id="sess-1", context=context, worktree=str(worktree), prompt="Feedback")

    assert calls == [(str(worktree), "claude-run-1-task-1-sess-1-abc123", "claude-send")]


def test_add_message_passes_kubectl_target_in_kubernetes_mode():
    manager = MagicMock(spec=ClaudeSessionManager)
    context = make_context(session_id="ses_abc", namespace="task-7-demo")
    adapter = ClaudeCodeAdapterService(
        context=context,
        settings=make_settings(claude_run_mode="kubernetes"),
        session_manager=manager,
    )

    adapter.add_message("ahoj")

    target = manager.send.call_args.kwargs["kubectl_target"]
    assert isinstance(target, KubectlExecTarget)
    assert target.namespace == "task-7-demo"


def test_deploy_runs_kubernetes_flow_when_mode_is_kubernetes(monkeypatch):
    invoked: list[str] = []

    def fake_super_deploy(self):  # noqa: ANN001
        invoked.append("deploy")
        return {"action": "deploy", "task_id": self.context.task_id, "status": "applied"}

    monkeypatch.setattr(KubernetesAdapterService, "deploy", fake_super_deploy)

    adapter = ClaudeCodeAdapterService(
        context=make_context(namespace="task-7-demo"),
        settings=make_settings(claude_run_mode="kubernetes"),
        session_manager=MagicMock(spec=ClaudeSessionManager),
    )

    result = adapter.deploy()

    assert invoked == ["deploy"]
    assert result["status"] == "applied"


def test_question_reply_is_not_implemented():
    adapter = ClaudeCodeAdapterService(
        context=make_context(),
        settings=make_settings(),
        session_manager=MagicMock(spec=ClaudeSessionManager),
    )

    with pytest.raises(NotImplementedError):
        adapter.question_reply("req-1", [["ano"]])


def test_close_aborts_session_and_skips_kubernetes(monkeypatch):
    manager = MagicMock(spec=ClaudeSessionManager)
    git_calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(ClaudeCodeAdapterService, "_repository_root", lambda self: Path("/var/www/repo"))
    monkeypatch.setattr(
        ClaudeCodeAdapterService,
        "_resolved_worktree_path",
        lambda self: Path("/srv/worktrees/task-1"),
    )

    def fake_succeeds(cwd: Path, *args: str) -> bool:
        # show-ref must succeed so branch is deleted
        return args[:1] == ("show-ref",) or args[:1] == ("worktree",)

    def fake_run_git(cwd: Path, *args: str) -> str:
        git_calls.append(args)
        return ""

    monkeypatch.setattr(KubernetesAdapterService, "_git_succeeds", staticmethod(fake_succeeds))
    monkeypatch.setattr(KubernetesAdapterService, "_run_git", staticmethod(fake_run_git))

    context = make_context(session_id="ses_abc")
    adapter = ClaudeCodeAdapterService(
        context=context,
        settings=make_settings(),
        session_manager=manager,
    )

    result = adapter.close()

    manager.abort.assert_called_once_with("ses_abc")
    manager.remove.assert_called_once_with("ses_abc")
    assert result["action"] == "close"
    assert result["branch"] == "task-1"
    assert result["worktree_removed"] is True
    assert result["branch_deleted"] is True
    # ensure manifest delete was NOT attempted (no kubectl calls; we only invoke git)
    assert all(call[0] != "kubectl" for call in git_calls)


# ---------------------------------------------------------------------------
# JSON-RPC integration via the FastAPI app
# ---------------------------------------------------------------------------


@pytest.fixture()
def claudecode_client(monkeypatch):
    """FastAPI client wired with a fake session manager (no real claude subprocess)."""

    fake_manager = MagicMock(spec=ClaudeSessionManager)
    fake_manager.start.return_value = "ses_abc123"

    monkeypatch.setattr("claude.api.ClaudeSessionManager", lambda settings: fake_manager)
    monkeypatch.setattr("claude.api.get_settings", lambda: make_settings())
    monkeypatch.setattr(
        ClaudeCodeAdapterService,
        "_persist_agentis_session_id",
        lambda self, session_id: None,
    )
    # Skip git worktree creation & repo introspection — return canned dicts.
    monkeypatch.setattr(
        ClaudeCodeAdapterService,
        "create_worktree",
        lambda self: {
            "action": "create_worktree",
            "task_id": self.context.task_id,
            "branch": "task-task-1",
            "base_branch": "master",
            "working_dir": "/srv/worktrees/task-1",
            "status": "created",
        },
    )

    app = create_app()
    return TestClient(app), fake_manager


def test_health_endpoint(claudecode_client):
    client, _manager = claudecode_client
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_start_dispatches_to_claude_session_manager(claudecode_client):
    client, manager = claudecode_client

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "start",
            "params": {
                "context": {
                    "run_id": "run-1",
                    "task_id": "task-1",
                    "title": "Implementace nove funkce",
                    "description": "Popis ukolu",
                    "project_slug": "agentis",
                    "working_dir": "/var/www/repo",
                    "adapter": {"agent": "build", "model": "claude-haiku-4-5-20251001"},
                }
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()["result"]
    steps = [step["action"] for step in payload["adapter"]["steps"]]
    assert steps == ["create_worktree", "deploy", "wait_ready", "start_session"]
    deploy_step = payload["adapter"]["steps"][1]
    assert deploy_step["status"] == "skipped"
    wait_step = payload["adapter"]["steps"][2]
    assert wait_step["url"] == "local://claude"
    session_step = payload["adapter"]["steps"][3]
    assert session_step["session_id"] == "ses_abc123"
    manager.start.assert_called_once()


def test_unknown_method_returns_404(claudecode_client):
    client, _manager = claudecode_client
    response = client.post(
        "/api",
        json={"jsonrpc": "2.0", "id": 1, "method": "missing", "params": {}},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# claudecode.py entrypoint
# ---------------------------------------------------------------------------


def test_claudecode_entrypoint_invokes_uvicorn(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_run(app: str, host: str, port: int, reload: bool) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["reload"] = reload

    monkeypatch.setattr("claudecode.uvicorn.run", fake_run)
    monkeypatch.setattr("claudecode.get_settings", lambda: make_settings(host="0.0.0.0", port=8002))

    from claudecode import main

    main()

    assert captured == {
        "app": "claude.api:app",
        "host": "0.0.0.0",
        "port": 8002,
        "reload": False,
    }


# ---------------------------------------------------------------------------
# ClaudeSessionManager small unit tests
# ---------------------------------------------------------------------------


def test_session_manager_remove_clears_state():
    manager = ClaudeSessionManager(settings=make_settings())
    # Inject a fake session directly to avoid spawning a real thread.
    manager._sessions["ses_x"] = MagicMock()
    manager.remove("ses_x")
    assert "ses_x" not in manager._sessions


def test_session_manager_abort_unknown_session_is_noop():
    manager = ClaudeSessionManager(settings=make_settings())
    # Should not raise
    manager.abort("ses_does_not_exist")


def test_session_manager_abort_kills_local_process_group(monkeypatch):
    manager = ClaudeSessionManager(settings=make_settings())
    sess = _ClaudeSession(
        session_id="ses_local",
        pending_key="pending",
        context=make_context(),
        worktree="/var/www/worktrees/task-1",
    )
    proc = SimpleNamespace(pid=12345, kill=MagicMock())
    sess.proc_holder["proc"] = proc
    manager._sessions["ses_local"] = sess

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("claude.session_manager.os.getpgid", lambda pid: 999)
    monkeypatch.setattr(
        "claude.session_manager.os.killpg",
        lambda pgid, sig: killed.append((pgid, sig)),
    )
    remote_pkill = MagicMock()
    monkeypatch.setattr(manager, "_remote_pkill_claude", remote_pkill)

    manager.abort("ses_local")

    assert sess.abort_event.is_set()
    assert killed == [(999, signal.SIGKILL)]
    proc.kill.assert_not_called()
    remote_pkill.assert_not_called()


def test_session_manager_abort_pkills_remote_claude_for_kubectl_session(monkeypatch):
    manager = ClaudeSessionManager(settings=make_settings(claude_run_mode="kubernetes"))
    target = KubectlExecTarget(namespace="task-9-demo", selector="deployment/opencode", container="opencode")
    sess = _ClaudeSession(
        session_id="ses_k8s",
        pending_key="pending",
        context=make_context(namespace="task-9-demo"),
        worktree="/var/www/worktrees/task-1",
        kubectl_target=target,
    )
    proc = SimpleNamespace(pid=42, kill=MagicMock())
    sess.proc_holder["proc"] = proc
    manager._sessions["ses_k8s"] = sess

    monkeypatch.setattr("claude.session_manager.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    captured: dict[str, Any] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("claude.session_manager.subprocess.run", fake_run)

    manager.abort("ses_k8s")

    assert sess.abort_event.is_set()
    proc.kill.assert_called_once()
    assert captured["args"] == [
        "kubectl",
        "-n",
        "task-9-demo",
        "exec",
        "deployment/opencode",
        "-c",
        "opencode",
        "--",
        "pkill",
        "-KILL",
        "-f",
        "claude --print",
    ]


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


def test_session_manager_start_waits_for_real_claude_session_id(monkeypatch):
    manager = ClaudeSessionManager(settings=make_settings())

    def fake_spawn_thread(sess, *, prompt, mapper, resume_id):
        assert mapper.session_id == ""
        manager._bind_session_id(sess, "claude-session-123")
        mapper.session_id = sess.session_id or ""

    monkeypatch.setattr(manager, "_spawn_thread", fake_spawn_thread)

    session_id = manager.start(
        context=make_context(),
        worktree="/var/www/worktrees/task-1",
        prompt="Popis ukolu",
    )

    assert session_id == "claude-session-123"
    assert "claude-session-123" in manager._sessions


def test_session_manager_bind_session_id_emits_session_created(monkeypatch):
    manager = ClaudeSessionManager(settings=make_settings(agentis_endpoint="http://agentis.local"))
    captured_calls: list[dict[str, Any]] = []
    sess = _ClaudeSession(
        session_id=None,
        pending_key="pending",
        context=make_context(title="Implementace nove funkce"),
        worktree="/var/www/worktrees/task-1",
    )
    manager._sessions["pending"] = sess

    monkeypatch.setattr(
        manager,
        "_agentis_call",
        lambda method, params: captured_calls.append({"method": method, "params": params}),
    )

    manager._bind_session_id(sess, "claude-session-123")

    assert captured_calls == [
        {
            "method": "session.session_created",
            "params": {
                "session": {
                    "id": "claude-session-123",
                    "parentID": None,
                    "title": "Implementace nove funkce",
                },
            },
        }
    ]


def test_session_manager_bind_existing_session_does_not_emit_session_created(monkeypatch):
    manager = ClaudeSessionManager(settings=make_settings(agentis_endpoint="http://agentis.local"))
    captured_calls: list[dict[str, Any]] = []
    sess = _ClaudeSession(
        session_id="claude-session-123",
        pending_key="pending",
        context=make_context(title="Implementace nove funkce"),
        worktree="/var/www/worktrees/task-1",
    )
    manager._sessions["claude-session-123"] = sess

    monkeypatch.setattr(
        manager,
        "_agentis_call",
        lambda method, params: captured_calls.append({"method": method, "params": params}),
    )

    manager._bind_session_id(sess, "claude-session-123")

    assert captured_calls == []


def test_session_manager_extract_final_text_returns_last_assistant_text():
    text = ClaudeSessionManager._extract_final_text(
        [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "ahoj"}]},
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "text", "text": "prvni cast"},
                    {"type": "tool", "tool": "bash"},
                    {"type": "text", "text": "druha cast"},
                ],
            },
        ]
    )
    assert text == "druha cast"


def test_session_manager_extract_final_text_empty_when_no_assistant():
    assert ClaudeSessionManager._extract_final_text([]) == ""
    assert (
        ClaudeSessionManager._extract_final_text([{"info": {"role": "user"}, "parts": [{"type": "text", "text": "x"}]}])
        == ""
    )


def test_session_manager_finish_actions_commit_pr_and_start_dev_server(monkeypatch, tmp_path: Path):
    captured_calls: list[dict[str, Any]] = []
    worktree = tmp_path / "worktrees" / "task-1"
    worktree.mkdir(parents=True)
    manager = ClaudeSessionManager(settings=make_settings(namespace_prefix="Task"))
    context = make_context(
        project_github_repo="example/repo",
        ide="vscode://file/[%WORKDIR%]?windowId=_blank",
    )
    sess = _ClaudeSession(
        session_id="sess-1",
        pending_key="pending",
        context=context,
        worktree=str(worktree),
    )

    monkeypatch.setattr(
        manager,
        "_agentis_call",
        lambda method, params: captured_calls.append({"method": method, "params": params}),
    )
    monkeypatch.setattr(
        manager,
        "_commit_session_changes",
        lambda context, worktree_path: {
            "status": "skipped",
            "reason": "clean_worktree",
            "working_dir": str(worktree_path),
        },
    )
    monkeypatch.setattr(
        manager,
        "_ensure_pull_request",
        lambda context, worktree_path: GithubPrResult(url="https://github.com/example/repo/pull/42", created=True),
    )
    monkeypatch.setattr(
        manager,
        "_start_dev_server",
        lambda sess: {"namespace": "task-1", "working_dir": sess.worktree},
    )

    attachments = manager._finish_session_actions(sess, "sess-1")

    assert attachments == [
        {
            "label": "Directory",
            "value": f"vscode://file/{worktree}?windowId=_blank",
            "type": "url",
        },
        {
            "label": "Pull Request",
            "value": "https://github.com/example/repo/pull/42/changes",
            "type": "url",
        },
        {
            "label": "Dev server",
            "type": "url",
            "value": "http://app-task-1.dev.agentis.cz",
        },
    ]
    adapter_events = [call["params"] for call in captured_calls if call["method"] == "run.adapter_event"]
    assert [(event["kind"], event["status"], event["message"]) for event in adapter_events] == [
        ("commit", "success", "Žádné změny ke commitnutí."),
        ("dev_server", "started", "Spouštím dev server."),
        ("dev_server", "success", "Dev server byl spuštěn."),
    ]


def test_session_manager_stream_adds_completion_attachments_and_actions(monkeypatch, tmp_path: Path):
    captured_calls: list[dict[str, Any]] = []
    diff_path = tmp_path / ".changes.diff"
    diff_content = "diff -ruN before/app.py after/app.py\n-old\n+new\n"
    diff_path.write_text(diff_content, encoding="utf-8")
    manager = ClaudeSessionManager(settings=make_settings())
    context = make_context(
        project_github_repo="example/repo",
        adapter=AdapterOptionsPayload(agent="build", model="claude-haiku-4-5-20251001", task_status=7),
    )
    sess = _ClaudeSession(
        session_id="sess-1",
        pending_key="pending",
        context=context,
        worktree="/var/www/worktrees/task-1",
        snapshot_key="snap-1",
    )
    mapper = ClaudeActivityMapper(prompt="Popis ukolu", mode="build", agent="claude", cwd=sess.worktree)
    captured_configs: list[Any] = []

    class FakeClaudeClient:
        last_cost_usd = 0.1
        last_usage = {"input_tokens": 1, "output_tokens": 2}

        def __init__(self, config: Any) -> None:
            captured_configs.append(config)
            self.session_id = None

        async def stream(self, prompt: str, *, on_proc_started=None):
            self.session_id = "sess-1"
            yield SimpleNamespace(type="session_start", data={"session_id": "sess-1", "model": "claude"})
            yield SimpleNamespace(type="text", data={"text": "Hotovo."})

    monkeypatch.setattr("claude.session_manager.ClaudeCodeClient", FakeClaudeClient)
    monkeypatch.setattr(
        manager,
        "_agentis_call",
        lambda method, params: captured_calls.append({"method": method, "params": params}),
    )
    monkeypatch.setattr(
        manager,
        "_finish_session_actions",
        lambda sess, session_ref: [
            {"label": "Pull Request", "value": "https://github.com/example/repo/pull/42/changes", "type": "url"}
        ],
    )
    monkeypatch.setattr(
        "claude.session_manager.collect_screenshot_images",
        lambda project_root: [{"name": "result.png", "content": "cG5n"}] if project_root == sess.worktree else [],
    )
    monkeypatch.setattr(
        "claude.session_manager.write_changes_diff_best_effort",
        lambda worktree, snapshot_key, label: type(
            "Result",
            (),
            {"status": "success", "diff_path": str(diff_path)},
        )(),
    )

    asyncio.run(manager._stream(sess, "Popis ukolu", mapper, resume_id=None))

    assert captured_configs[0].env == {"IS_SANDBOX": "1", "AGENTIS_URL": "http://adapter.internal:8002"}
    comment_calls = [call for call in captured_calls if call["method"] == "task.add_agent_comment"]
    assert len(comment_calls) == 1
    assert comment_calls[0]["params"] == {
        "session_id": "sess-1",
        "body": "Hotovo.",
        "attachments": [
            {
                "label": "Pull Request",
                "value": "https://github.com/example/repo/pull/42/changes",
                "type": "url",
            },
            {"label": "Changes diff", "value": diff_content, "type": "diff"},
        ],
        "images": [{"name": "result.png", "content": "cG5n"}],
        "artifacts": [],
        "status": 7,
        "comment_type": "primary",
        "actions": ClaudeSessionManager._completion_actions(context),
    }


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
