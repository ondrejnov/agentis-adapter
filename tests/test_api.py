from pathlib import Path
from types import SimpleNamespace
from typing import cast
from typing import Any

import pytest

from common.config import Settings
from opencode.api import create_app, _DISPATCH
from common.models import (
    AgentExecutionContextPayload,
    AdapterOptionsPayload,
    TaskStatus,
)
from common.git_adapter import GitAdapterService
from common.artifacts.expected import collect_expected_artifacts
from common.namespaces import dev_server_url_for_context, namespace_for_context
from common.rpc.jsonrpc import AgentJsonRpcService
from tests.support import RpcTestClient


def make_client(service: AgentJsonRpcService | None = None) -> RpcTestClient:
    app = create_app()
    if service is not None:
        app.state.agent_jsonrpc_service = service
        app.state.session_registry = service.session_registry
    return RpcTestClient(app, _DISPATCH)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8001,
        "worktree_root": Path("/var/www/worktrees"),
        "public_base_url": "http://adapter.internal:8001",
        "agentis_endpoint": "http://10.0.0.205:8891",
        "agentis_token": "1234",
    }
    values.update(overrides)
    return Settings(**values)


def test_collect_expected_artifacts_upload_payload(tmp_path):
    report = tmp_path / "dist" / "report.json"
    report.parent.mkdir()
    report.write_text('{"ok": true}', encoding="utf-8")

    context = AgentExecutionContextPayload(
        run_id="run-1",
        task_id="task-1",
        title="Task",
        expected_artifacts=[{"path": "dist/report.json", "name": "report"}],
    )

    assert collect_expected_artifacts(context, tmp_path) == [
        {
            "name": "report",
            "filename": "report.json",
            "content": "eyJvayI6IHRydWV9",
        }
    ]


def test_collect_expected_artifacts_ignores_paths_outside_root(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    context = AgentExecutionContextPayload(
        run_id="run-1",
        task_id="task-1",
        title="Task",
        expected_artifacts=["../outside.txt"],
    )

    assert collect_expected_artifacts(context, tmp_path) == []


def fake_agentis_client_factory(captured_calls: list[dict[str, Any]]):
    class FakeAgentisClient:
        def __init__(self, endpoint: str, token: str | None = None, timeout: float = 15.0) -> None:
            self.endpoint = endpoint
            self.token = token
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            pass

        def call(self, method: str, params: Any = None, *, request_id: Any | None = None) -> Any:
            captured_calls.append(
                {
                    "endpoint": self.endpoint,
                    "token": self.token,
                    "timeout": self.timeout,
                    "request_id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            return {"ok": True, "method": method}

    return FakeAgentisClient


def make_start_params(run_id: str = "run-1") -> dict[str, Any]:
    return {
        "context": {
            "run_id": run_id,
            "task_id": "task-1",
            "title": "Implementace nove funkce",
            "description": "Popis ukolu",
            "project_slug": "agentis",
            "working_dir": "/var/www/repo",
            "adapter": {"agent": "build", "model": "gpt-5.4"},
        }
    }


def test_create_worktree_uses_adapter_branch_override(monkeypatch, tmp_path):
    repository_root = Path("/var/www/repo")
    git_calls: list[tuple[Path, tuple[str, ...]]] = []
    succeeds_calls: list[tuple[Path, tuple[str, ...]]] = []

    def fake_git_succeeds(cwd: Path, *args: str) -> bool:
        succeeds_calls.append((cwd, args))
        return cwd == repository_root and args == ("rev-parse", "--verify", "--quiet", "master^{commit}")

    def fake_run_git(cwd: Path, *args: str) -> str:
        git_calls.append((cwd, args))
        if cwd == Path("/var/www/repo") and args == ("rev-parse", "--show-toplevel"):
            return str(repository_root)
        if cwd == repository_root and args == (
            "worktree",
            "add",
            "-b",
            "feature/custom",
            str(tmp_path / "task-1"),
            "master",
        ):
            return ""
        raise AssertionError(f"Unexpected git command: cwd={cwd}, args={args}")

    monkeypatch.setattr(GitAdapterService, "_git_succeeds", staticmethod(fake_git_succeeds))
    monkeypatch.setattr(GitAdapterService, "_run_git", staticmethod(fake_run_git))

    service = GitAdapterService(
        AgentExecutionContextPayload(
            run_id="run-1",
            task_id="task-1",
            title="Implementace nove funkce",
            project_slug="agentis",
            working_dir=str(repository_root),
            adapter=AdapterOptionsPayload(branch=" feature/custom "),
        ),
        make_settings(worktree_root=tmp_path),
    )

    assert service.create_worktree() == {
        "action": "create_worktree",
        "task_id": "task-1",
        "branch": "feature/custom",
        "base_branch": "master",
        "working_dir": str(tmp_path / "task-1"),
        "status": "created",
    }
    assert git_calls == [
        (Path("/var/www/repo"), ("rev-parse", "--show-toplevel")),
        (
            repository_root,
            ("worktree", "add", "-b", "feature/custom", str(tmp_path / "task-1"), "master"),
        ),
    ]
    assert succeeds_calls == [
        (repository_root, ("show-ref", "--verify", "--quiet", "refs/heads/feature/custom")),
        (repository_root, ("rev-parse", "--verify", "--quiet", "master^{commit}")),
    ]


def test_namespace_for_context_uses_task_number_title_and_prefix():
    context = AgentExecutionContextPayload(
        run_id="run-1",
        task_id="019dc3cd-3bcb",
        task_number=17,
        title="Implementace nove funkce",
        project_slug="agentis",
        working_dir="/var/www/repo",
    )

    namespace = namespace_for_context(
        context,
        make_settings(namespace_prefix="Task"),
    )

    assert namespace == "task-17-implementace-nove-fu"


def test_repository_root_uses_vscode_working_dir(monkeypatch):
    captured: dict[str, Path] = {}

    context = AgentExecutionContextPayload(
        run_id="run-1",
        task_id="task-1",
        title="VS Code task",
        working_dir="/workspace/open-project",
        adapter=AdapterOptionsPayload(scope="project"),
    )
    service = GitAdapterService(context, make_settings())

    def fake_run_git(cwd: Path, *args: str) -> str:
        captured["cwd"] = cwd
        return "/workspace/open-project"

    monkeypatch.setattr(GitAdapterService, "_run_git", staticmethod(fake_run_git))

    assert service._repository_root() == Path("/workspace/open-project")
    assert captured["cwd"] == Path("/workspace/open-project")


def test_namespace_for_context_normalizes_configured_prefix_and_title():
    context = AgentExecutionContextPayload(
        run_id="run-1",
        task_id="task-1",
        task_number=8,
        title="Zavřít prostředí",
        project_slug="agentis",
        working_dir="/var/www/repo",
    )

    namespace = namespace_for_context(
        context,
        make_settings(namespace_prefix="Agent ENV"),
    )

    assert namespace == "agent-env-8-zavrit-prostredi"


def test_dev_server_url_for_context_uses_kubernetes_namespace():
    context = AgentExecutionContextPayload(
        run_id="run-1",
        task_id="019dc3cd-3bcb",
        task_number=17,
        title="Implementace nove funkce",
        project_slug="agentis",
        working_dir="/var/www/repo",
    )

    url = dev_server_url_for_context(
        context,
        make_settings(namespace_prefix="Task"),
    )

    assert url == "http://app-task-17-implementace-nove-fu.dev.agentis.cz"


def test_project_scope_namespace_uses_project_slug():
    context = AgentExecutionContextPayload(
        run_id="run-1",
        task_id="task-1",
        task_number=17,
        title="Implementace nove funkce",
        project_slug="Agentis Core",
        working_dir="/var/www/repo",
        adapter=AdapterOptionsPayload(scope="project"),
    )

    namespace = namespace_for_context(context, make_settings(namespace_prefix="Task"))
    dev_server_url = dev_server_url_for_context(
        context, make_settings(namespace_prefix="Task")
    )

    assert namespace == "project-agentis-core"
    assert dev_server_url == "http://app-project-agentis-core.dev.agentis.cz"


def expected_completion_actions() -> list[dict[str, Any]]:
    return [
        {
            "title": "Git merge",
            "prompt": "Sloučit změny z task větve do hlavní větve.",
            "adapter_method": "git_merge",
            "continue_previous_run": False,
        },
        {
            "title": "Zavřít prostředí",
            "prompt": "Uklidit prostředí, worktree a task větev.",
            "adapter_method": "close",
            "continue_previous_run": False,
        },
    ]


def strip_event_id(params: dict[str, Any]) -> dict[str, Any]:
    result = dict(params)
    event_id = result.pop("event_id", None)
    assert isinstance(event_id, str)
    return result


def test_jsonrpc_happy_path_flow_runs_adapter_without_dry_run():
    events: list[tuple[str, str, str | None]] = []

    class FakeAdapter:
        def post_agentis_event(
            self,
            *,
            kind: str,
            status: str,
            event_id: str | None = None,
            message: str | None = None,
            data: dict | None = None,
        ) -> None:
            events.append((kind, status, message))

        def create_worktree(self) -> dict[str, str]:
            return {"action": "create_worktree", "status": "created"}

        def deploy(self) -> dict[str, str]:
            return {"action": "deploy", "status": "applied"}

        def wait_ready(self) -> dict[str, str]:
            return {"action": "wait_ready", "status": "ready", "url": "http://pod"}

        def start_session(self, pod_url: str, fork_from_session_id: str | None = None) -> dict[str, str | None]:
            return {
                "action": "start_session",
                "session_id": "sess-1",
                "pod_url": pod_url,
                "fork_from_session_id": fork_from_session_id,
                "snapshot_key": "snap-start",
            }

        def add_message(self, message: str, pod_url: str) -> dict[str, str]:
            return {"action": "add_message", "message": message, "pod_url": pod_url, "snapshot_key": "snap-feedback"}

        def question_reply(self, request_id: str, answers: list[list[str]], pod_url: str) -> dict[str, Any]:
            return {"action": "question_reply", "request_id": request_id, "answers": answers, "pod_url": pod_url}

        def restore_snapshot(self, snapshot_key: str) -> dict[str, str]:
            return {"action": "undo", "snapshot_key": snapshot_key}

    service = AgentJsonRpcService(
        settings=make_settings(),
        adapter_factory=cast(Any, lambda context: FakeAdapter()),
    )
    client = make_client(service)

    start_response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "start",
            "params": make_start_params(),
        },
    )

    assert start_response.status_code == 200
    start_payload = start_response.json()["result"]
    assert start_payload["run"]["run_id"] == "run-1"
    assert "dry_run" not in start_payload["run"]
    assert "dry_run" not in start_payload["run"]["events"][0]["payload"]
    assert start_payload["adapter"]["executed"] is True
    assert [step["action"] for step in start_payload["adapter"]["steps"]] == [
        "create_worktree",
        "deploy",
        "wait_ready",
        "start_session",
    ]
    assert events == [
        ("create_worktree", "success", "Git worktree je připravený."),
        ("deploy", "started", "Připravuji prostředí."),
        ("deploy", "success", "Prostředí je připravené."),
        ("wait_ready", "started", "Čekám na připravenost prostředí."),
        ("wait_ready", "success", "Prostředí běží."),
        ("start_session", "started", "Zakládám Agent session."),
        ("start_session", "success", "Agent session byla založena."),
    ]
    assert "super-secret-token" not in start_response.text
    assert service.session_registry.get_snapshot_key("sess-1") == "snap-start"

    message_response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "add_message",
            "params": {
                "run_id": "run-1",
                "context": start_payload["run"]["context"],
                "message": "Ahoj",
                "role": "user",
            },
        },
    )
    assert message_response.status_code == 200
    message_payload = message_response.json()["result"]
    assert message_payload["run"]["events"][0]["kind"] == "message"
    assert [step["action"] for step in message_payload["adapter"]["steps"]] == [
        "create_worktree",
        "deploy",
        "wait_ready",
        "add_message",
    ]
    assert service.session_registry.get_snapshot_key("sess-1") == "snap-feedback"

    undo_response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "undo",
            "params": {"context": message_payload["run"]["context"]},
        },
    )
    assert undo_response.status_code == 200
    undo_payload = undo_response.json()["result"]
    assert undo_payload["adapter"]["steps"] == [{"action": "undo", "snapshot_key": "snap-feedback"}]

    question_response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "question",
            "params": {
                "run_id": "run-1",
                "context": start_payload["run"]["context"],
                "request_id": "que_123",
                "answers": [["Ano"], ["Custom odpoved"]],
            },
        },
    )
    assert question_response.status_code == 200
    question_payload = question_response.json()["result"]
    assert question_payload["run"]["events"][0]["kind"] == "question"
    assert [step["action"] for step in question_payload["adapter"]["steps"]] == [
        "create_worktree",
        "deploy",
        "wait_ready",
        "question_reply",
    ]
    assert question_payload["adapter"]["steps"][-1]["request_id"] == "que_123"
    assert question_payload["adapter"]["steps"][-1]["answers"] == [["Ano"], ["Custom odpoved"]]

    approve_response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "approve",
            "params": {"run_id": "run-1", "approved": True, "comment": "Pokracuj"},
        },
    )
    assert approve_response.status_code == 200
    assert approve_response.json()["result"]["approved"] is True


def test_jsonrpc_start_runs_agentis_init_when_adapter_requires_it():
    class FakeAdapter:
        requires_agentis_init = True

        def create_worktree(self) -> dict[str, str]:
            return {"action": "create_worktree", "status": "created"}

        def init_agentis(self) -> dict[str, str]:
            return {"action": "init_agentis", "status": "copied"}

        def deploy(self) -> dict[str, str]:
            return {"action": "deploy", "status": "applied"}

        def wait_ready(self) -> dict[str, str]:
            return {"action": "wait_ready", "status": "ready", "url": "http://pod"}

        def start_session(self, pod_url: str, fork_from_session_id: str | None = None) -> dict[str, str | None]:
            return {
                "action": "start_session",
                "session_id": "sess-1",
                "pod_url": pod_url,
                "fork_from_session_id": fork_from_session_id,
            }

    service = AgentJsonRpcService(
        settings=make_settings(),
        adapter_factory=cast(Any, lambda context: FakeAdapter()),
    )
    response = make_client(service).post(
        "/api",
        json={"jsonrpc": "2.0", "id": 1, "method": "start", "params": make_start_params()},
    )

    assert response.status_code == 200
    assert [step["action"] for step in response.json()["result"]["adapter"]["steps"]] == [
        "create_worktree",
        "init_agentis",
        "deploy",
        "wait_ready",
        "start_session",
    ]


def test_start_accepts_extended_agent_execution_context_schema():
    class FakeAdapter:
        def create_worktree(self) -> dict[str, str]:
            return {"action": "create_worktree", "status": "created"}

        def deploy(self) -> dict[str, str]:
            return {"action": "deploy", "status": "applied"}

        def wait_ready(self) -> dict[str, str]:
            return {"action": "wait_ready", "status": "ready", "url": "http://pod"}

        def start_session(self, pod_url: str, fork_from_session_id: str | None = None) -> dict[str, str | None]:
            return {
                "action": "start_session",
                "session_id": "sess-1",
                "pod_url": pod_url,
                "fork_from_session_id": fork_from_session_id,
            }

    service = AgentJsonRpcService(
        settings=make_settings(),
        adapter_factory=cast(Any, lambda context: FakeAdapter()),
    )
    client = make_client(service)

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "start",
            "params": {
                "context": {
                    **make_start_params()["context"],
                    "project_id": "proj-1",
                    "project_documentation": "https://docs.example.test/project",
                    "task_status": 2,
                    "task_number": 17,
                    "task_priority": 3,
                    "parent_task_id": 12,
                    "agent_id": "agent-1",
                    "agent_title": "Builder",
                    "agent_prompt": "Follow repository instructions.",
                    "adapter": {"task_status": 7},
                    "comments": [
                        {
                            "id": "comment-1",
                            "author_type": "user",
                            "author_name": "Alice",
                            "body": "Prosim dopln dokumentaci.",
                            "created": "2026-04-19T10:00:00Z",
                            "updated": "2026-04-19T10:05:00Z",
                            "run_id": "run-1",
                            "attachments": [{"path": "/tmp/note.txt", "filename": "note.txt"}],
                        }
                    ],
                }
            },
        },
    )

    assert response.status_code == 200
    context = response.json()["result"]["run"]["context"]
    assert context["project_id"] == "proj-1"
    assert context["project_documentation"] == "https://docs.example.test/project"
    assert context["task_status"] == 2
    assert context["task_number"] == 17
    assert context["task_priority"] == 3
    assert context["parent_task_id"] == 12
    assert context["agent_id"] == "agent-1"
    assert context["agent_title"] == "Builder"
    assert context["agent_prompt"] == "Follow repository instructions."
    assert context["adapter"]["task_status"] == 7
    assert context["comments"] == [
        {
            "id": "comment-1",
            "author_type": "user",
            "author_name": "Alice",
            "body": "Prosim dopln dokumentaci.",
            "created": "2026-04-19T10:00:00Z",
            "updated": "2026-04-19T10:05:00Z",
            "run_id": "run-1",
            "attachments": [{"path": "/tmp/note.txt", "filename": "note.txt"}],
        }
    ]


def test_close_forwards_cleanup_comment_and_removes_session(monkeypatch):
    captured_calls: list[dict[str, Any]] = []

    class FakeAdapter:
        def close(self) -> dict[str, str]:
            return {"action": "close", "status": "cleaned"}

    class FakeAgentisClient:
        def __init__(self, endpoint: str, token: str | None = None, timeout: float = 15.0) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            pass

        def call(self, method: str, params: Any = None, *, request_id: Any | None = None) -> Any:
            captured_calls.append({"request_id": request_id, "method": method, "params": params})
            return {"ok": True}

    monkeypatch.setattr("common.rpc.jsonrpc.AgentisJsonRpcClient", FakeAgentisClient)

    service = AgentJsonRpcService(
        settings=make_settings(),
        adapter_factory=cast(Any, lambda context: FakeAdapter()),
    )
    service.session_registry.register(
        "sess-1",
        AgentExecutionContextPayload.model_validate(
            {
                **make_start_params()["context"],
                "session_id": "sess-1",
            }
        ),
    )
    client = make_client(service)

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "close",
            "params": {
                "context": {
                    **make_start_params()["context"],
                    "session_id": "sess-1",
                }
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["result"]["adapter"]["steps"] == [{"action": "close", "status": "cleaned"}]
    assert captured_calls == [
        {
            "request_id": captured_calls[0]["request_id"],
            "method": "task.add_agent_comment",
            "params": {
                "run_id": "run-1",
                "body": "Prostředí a git větev byly uklizeny.",
                "status": TaskStatus.CANCELLED,
            },
        }
    ]
    assert isinstance(captured_calls[0]["request_id"], str)
    assert service.session_registry.get("sess-1") is None


def test_git_merge_forwards_done_status_and_removes_session(monkeypatch):
    captured_calls: list[dict[str, Any]] = []

    class FakeAdapter:
        def git_merge(self, message: str | None = None) -> dict[str, str | None]:
            return {"action": "git_merge", "message": message, "conflict_resolution_output": "resolved by AI"}

        def close(self) -> dict[str, str]:
            return {"action": "close", "status": "cleaned"}

    class FakeAgentisClient:
        def __init__(self, endpoint: str, token: str | None = None, timeout: float = 15.0) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            pass

        def call(self, method: str, params: Any = None, *, request_id: Any | None = None) -> Any:
            captured_calls.append({"request_id": request_id, "method": method, "params": params})
            return {"ok": True}

    monkeypatch.setattr("common.rpc.jsonrpc.AgentisJsonRpcClient", FakeAgentisClient)

    service = AgentJsonRpcService(
        settings=make_settings(),
        adapter_factory=cast(Any, lambda context: FakeAdapter()),
    )
    service.session_registry.register(
        "sess-1",
        AgentExecutionContextPayload.model_validate(
            {
                **make_start_params()["context"],
                "session_id": "sess-1",
            }
        ),
    )
    client = make_client(service)

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "git_merge",
            "params": {
                "context": {
                    **make_start_params()["context"],
                    "session_id": "sess-1",
                },
                "message": "Merge hotov",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["result"]["adapter"]["steps"] == [{"action": "close", "status": "cleaned"}]
    assert captured_calls == [
        {
            "request_id": captured_calls[0]["request_id"],
            "method": "task.add_agent_comment",
            "params": {
                "run_id": "run-1",
                "body": (
                    "✔️ Zamergoval jsem task větev do hlavní větve a uklidil prostředí."
                    "\n\nMerge narazil na git conflict, který jsem řešil přes AI resolver."
                    "\n\nVýsledek AI resolveru:\n\n```\nresolved by AI\n```"
                ),
                "status": TaskStatus.DONE,
            },
        }
    ]
    assert isinstance(captured_calls[0]["request_id"], str)
    assert service.session_registry.get("sess-1") is None


def test_abort_stops_opencode_session_and_removes_session():
    events: list[tuple[str, str, str | None]] = []

    class FakeAdapter:
        def post_agentis_event(
            self,
            *,
            kind: str,
            status: str,
            event_id: str | None = None,
            message: str | None = None,
            data: dict | None = None,
        ) -> None:
            events.append((kind, status, message))

        def abort(self, session_id: str) -> dict[str, str]:
            return {"action": "abort", "session_id": session_id, "status": "aborted"}

    service = AgentJsonRpcService(
        settings=make_settings(),
        adapter_factory=cast(Any, lambda context: FakeAdapter()),
    )
    service.session_registry.register(
        "sess-1",
        AgentExecutionContextPayload.model_validate(
            {
                **make_start_params()["context"],
                "session_id": "sess-1",
            }
        ),
    )
    client = make_client(service)

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "abort",
            "params": {
                "context": {
                    **make_start_params()["context"],
                    "session_id": "sess-1",
                }
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()["result"]
    assert payload["run"]["opencode_session_id"] == "sess-1"
    assert payload["adapter"]["steps"] == [{"action": "abort", "session_id": "sess-1", "status": "aborted"}]
    assert events == [
        ("abort", "started", "Zastavuji bezici OpenCode session."),
        ("abort", "success", "OpenCode session byla zastavena."),
    ]
    assert service.session_registry.get("sess-1") is None


def test_abort_requires_session_id_in_context():
    client = make_client(AgentJsonRpcService(settings=make_settings(), adapter_factory=cast(Any, lambda context: None)))

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "abort",
            "params": {
                "context": make_start_params()["context"],
            },
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": 400,
        "message": "Context must include session_id to abort session",
    }


def test_add_message_registers_session_context():
    class FakeAdapter:
        def create_worktree(self) -> dict[str, str]:
            return {"action": "create_worktree", "status": "created"}

        def deploy(self) -> dict[str, str]:
            return {"action": "deploy", "status": "applied"}

        def wait_ready(self) -> dict[str, str]:
            return {"action": "wait_ready", "status": "ready", "url": "http://pod"}

        def add_message(self, message: str, pod_url: str) -> dict[str, str]:
            return {"action": "add_message", "message": message, "pod_url": pod_url, "snapshot_key": "snap-feedback"}

    service = AgentJsonRpcService(
        settings=make_settings(),
        adapter_factory=cast(Any, lambda context: FakeAdapter()),
    )
    client = make_client(service)
    context = make_start_params()["context"]
    context["session_id"] = "sess-1"

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 20,
            "method": "add_message",
            "params": {
                "run_id": "run-1",
                "context": context,
                "message": "Ahoj",
                "role": "user",
            },
        },
    )

    assert response.status_code == 200
    registered = service.session_registry.get("sess-1")
    assert registered is not None
    assert registered.run_id == "run-1"
    assert service.session_registry.get_snapshot_key("sess-1") == "snap-feedback"


def test_add_message_rejects_mismatched_context_run_id():
    client = make_client(AgentJsonRpcService(settings=make_settings(), adapter_factory=cast(Any, lambda context: None)))
    context = make_start_params(run_id="run-2")["context"]

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 21,
            "method": "add_message",
            "params": {
                "run_id": "run-1",
                "context": context,
                "message": "Ahoj",
                "role": "user",
            },
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


def test_start_accepts_null_for_defaulted_context_fields():
    class FakeAdapter:
        def create_worktree(self) -> dict[str, str]:
            return {"action": "create_worktree", "status": "created"}

        def deploy(self) -> dict[str, str]:
            return {"action": "deploy", "status": "applied"}

        def wait_ready(self) -> dict[str, str]:
            return {"action": "wait_ready", "status": "ready", "url": "http://pod"}

        def start_session(self, pod_url: str, fork_from_session_id: str | None = None) -> dict[str, str | None]:
            return {
                "action": "start_session",
                "session_id": "sess-1",
                "pod_url": pod_url,
                "fork_from_session_id": fork_from_session_id,
            }

    service = AgentJsonRpcService(
        settings=make_settings(),
        adapter_factory=cast(Any, lambda context: FakeAdapter()),
    )
    client = make_client(service)

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "start",
            "params": {
                "context": {
                    **make_start_params()["context"],
                    "project_slug": None,
                    "base_branch": None,
                    "working_dir": None,
                }
            },
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["run"]["context"]["project_slug"] == "agentis"
    assert result["run"]["context"]["base_branch"] == "master"
    assert result["run"]["context"]["working_dir"] == "/var/www/agentis-general"
    assert result["run"]["events"][0]["payload"]["project_slug"] == "agentis"


def test_start_rejects_removed_dry_run_param():
    client = make_client()

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "start",
            "params": {
                **make_start_params(),
                "dry_run": False,
            },
        },
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == -32602
    assert any(item["loc"] == ["dry_run"] for item in payload["error"]["data"])


def test_jsonrpc_unknown_method_returns_error():
    client = make_client()
    response = client.post("/api", json={"jsonrpc": "2.0", "id": 1, "method": "missing", "params": {}})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32601


def test_jsonrpc_http_dispatch_preserves_invalid_params_shape():
    client = make_client()

    response = client.post(
        "/api",
        json={"jsonrpc": "2.0", "id": "bad-start", "method": "start", "params": {"context": {}}},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == "bad-start"
    assert payload["error"]["code"] == -32602
    assert payload["error"]["message"] == "Invalid params"
    assert isinstance(payload["error"]["data"], list)


def test_jsonrpc_internal_errors_are_logged_to_stderr(capsys):
    events: list[tuple[str, str, str | None]] = []

    class ExplodingAdapter:
        def post_agentis_event(
            self,
            *,
            kind: str,
            status: str,
            event_id: str | None = None,
            message: str | None = None,
            data: dict | None = None,
        ) -> None:
            events.append((kind, status, message))

        def create_worktree(self) -> dict[str, str]:
            raise RuntimeError("boom")

    service = AgentJsonRpcService(
        settings=make_settings(),
        adapter_factory=cast(Any, lambda context: ExplodingAdapter()),
    )
    client = make_client(service)

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "start",
            "params": make_start_params(),
        },
    )

    assert response.status_code == 500
    assert events == [
        ("create_worktree", "failed", "boom"),
    ]
    stderr = capsys.readouterr().err
    assert "JSON-RPC method failed" in stderr
    assert "RuntimeError: boom" in stderr
    assert "AgentJsonRpcException: Adapter error: boom" in stderr


def test_project_scope_uses_current_branch_workspace(monkeypatch):
    repository_root = Path("/var/www/repo")

    def fake_run_git(cwd: Path, *args: str) -> str:
        if cwd == repository_root and args == ("branch", "--show-current"):
            return "feature/current"
        raise AssertionError(f"Unexpected git command: cwd={cwd}, args={args}")

    monkeypatch.setattr(GitAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(GitAdapterService, "_run_git", staticmethod(fake_run_git))

    service = GitAdapterService(
        AgentExecutionContextPayload(
            run_id="run-1",
            task_id="task-1",
            title="Implementace nove funkce",
            project_slug="Agentis Core",
            working_dir=str(repository_root),
            adapter=AdapterOptionsPayload(scope="project"),
        ),
        make_settings(worktree_root=Path("/srv/worktrees")),
    )

    assert service.create_worktree() == {
        "action": "create_worktree",
        "task_id": "task-1",
        "branch": "feature/current",
        "base_branch": "master",
        "working_dir": "/var/www/repo",
        "status": "skipped",
        "reason": "project_scope",
    }


def test_project_scope_allows_working_dir_without_git(tmp_path):
    service = GitAdapterService(
        AgentExecutionContextPayload(
            run_id="run-1",
            task_id="task-1",
            title="Implementace nove funkce",
            project_slug="Agentis Core",
            working_dir=str(tmp_path),
            adapter=AdapterOptionsPayload(scope="project"),
        ),
        make_settings(),
    )

    assert service.create_worktree() == {
        "action": "create_worktree",
        "task_id": "task-1",
        "branch": None,
        "base_branch": "master",
        "working_dir": str(tmp_path),
        "status": "skipped",
        "reason": "project_scope",
    }
    assert service.git_merge() == {
        "action": "git_merge",
        "task_id": "task-1",
        "branch": None,
        "base_branch": "master",
        "status": "skipped",
        "reason": "project_scope",
        "repository_root": str(tmp_path),
    }


def test_project_scope_skips_git_merge_and_close_cleanup(monkeypatch):
    repository_root = Path("/var/www/repo")

    def fake_run_git(cwd: Path, *args: str) -> str:
        if cwd == repository_root and args == ("branch", "--show-current"):
            return "feature/current"
        raise AssertionError(f"Unexpected git command: cwd={cwd}, args={args}")

    monkeypatch.setattr(GitAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(GitAdapterService, "_run_git", staticmethod(fake_run_git))

    service = GitAdapterService(
        AgentExecutionContextPayload(
            run_id="run-1",
            task_id="task-1",
            title="Implementace nove funkce",
            project_slug="Agentis Core",
            working_dir=str(repository_root),
            adapter=AdapterOptionsPayload(scope="project"),
        ),
        make_settings(),
    )

    assert service.git_merge() == {
        "action": "git_merge",
        "task_id": "task-1",
        "branch": "feature/current",
        "base_branch": "master",
        "status": "skipped",
        "reason": "project_scope",
        "repository_root": "/var/www/repo",
    }
    assert service.close() == {
        "action": "close",
        "task_id": "task-1",
        "branch": "feature/current",
        "base_branch": "master",
        "status": "skipped",
        "reason": "project_scope",
        "repository_root": "/var/www/repo",
        "worktree_removed": False,
        "branch_deleted": False,
    }


def test_git_merge_pushes_base_branch_after_rebase_and_fast_forward(monkeypatch):
    repository_root = Path("/var/www/repo")
    worktree_path = Path("/var/www/worktrees/task-1")
    git_calls: list[tuple[str, ...]] = []
    succeeds_calls: list[tuple[Path, tuple[str, ...]]] = []

    def fake_git_succeeds(cwd: Path, *args: str) -> bool:
        succeeds_calls.append((cwd, args))
        return (
            (cwd == repository_root and args == ("show-ref", "--verify", "--quiet", "refs/heads/task-1"))
            or (cwd == worktree_path and args == ("rev-parse", "--is-inside-work-tree"))
            or (cwd == repository_root and args == ("config", "--get", "branch.master.remote"))
            or (cwd == repository_root and args == ("checkout", "feature-before-merge"))
        )

    def fake_run_git(cwd: Path, *args: str) -> str:
        git_calls.append(args)
        if cwd == worktree_path and args == ("branch", "--show-current"):
            return "task-1"
        if cwd == repository_root and args == ("branch", "--show-current"):
            return "feature-before-merge"
        if cwd == repository_root and args == ("config", "--get", "branch.master.remote"):
            return "origin"
        if cwd == repository_root and args == ("fetch", "origin", "master"):
            return ""
        if cwd == worktree_path and args == ("rebase", "refs/remotes/origin/master"):
            return ""
        if cwd == repository_root and args == ("rebase", "task-1"):
            return ""
        if cwd == repository_root and args == ("rev-parse", "HEAD"):
            return "merge123"
        if cwd == repository_root and args == ("push", "origin", "master:refs/heads/master"):
            return ""
        raise AssertionError(f"Unexpected git command: cwd={cwd}, args={args}")

    monkeypatch.setattr(GitAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(GitAdapterService, "_resolved_worktree_path", lambda self: worktree_path)
    monkeypatch.setattr(GitAdapterService, "_git_succeeds", staticmethod(fake_git_succeeds))
    monkeypatch.setattr(GitAdapterService, "_run_git", staticmethod(fake_run_git))

    service = GitAdapterService(
        AgentExecutionContextPayload(
            run_id="run-1",
            task_id="task-1",
            title="Implementace nove funkce",
            project_slug="agentis",
            working_dir="/var/www/repo",
        ),
        make_settings(),
    )

    result = service.git_merge()

    assert result == {
        "action": "git_merge",
        "task_id": "task-1",
        "branch": "task-1",
        "base_branch": "master",
        "merge_commit": "merge123",
        "commit": "merge123",
        "push_remote": "origin",
        "repository_root": "/var/www/repo",
    }
    assert git_calls == [
        ("branch", "--show-current"),
        ("branch", "--show-current"),
        ("config", "--get", "branch.master.remote"),
        ("fetch", "origin", "master"),
        ("rebase", "refs/remotes/origin/master"),
        ("rebase", "task-1"),
        ("rev-parse", "HEAD"),
        ("push", "origin", "master:refs/heads/master"),
    ]
    assert succeeds_calls == [
        (repository_root, ("show-ref", "--verify", "--quiet", "refs/heads/task-1")),
        (worktree_path, ("rev-parse", "--is-inside-work-tree")),
        (repository_root, ("config", "--get", "branch.master.remote")),
        (repository_root, ("checkout", "feature-before-merge")),
    ]


def test_git_merge_aborts_failed_rebase_when_conflict_resolver_fails(monkeypatch):
    repository_root = Path("/var/www/repo")
    worktree_path = Path("/var/www/worktrees/task-1")
    git_calls: list[tuple[Path, tuple[str, ...]]] = []
    succeeds_calls: list[tuple[Path, tuple[str, ...]]] = []
    event_calls: list[dict[str, Any]] = []

    def fake_git_succeeds(cwd: Path, *args: str) -> bool:
        succeeds_calls.append((cwd, args))
        return (
            (cwd == repository_root and args == ("show-ref", "--verify", "--quiet", "refs/heads/task-1"))
            or (cwd == worktree_path and args == ("rev-parse", "--is-inside-work-tree"))
            or (cwd == repository_root and args == ("config", "--get", "branch.master.remote"))
            or (cwd == worktree_path and args == ("rebase", "--abort"))
            or (cwd == repository_root and args == ("checkout", "feature-before-merge"))
        )

    def fake_run_git(cwd: Path, *args: str) -> str:
        git_calls.append((cwd, args))
        if cwd == worktree_path and args == ("branch", "--show-current"):
            return "task-1"
        if cwd == repository_root and args == ("branch", "--show-current"):
            return "feature-before-merge"
        if cwd == repository_root and args == ("config", "--get", "branch.master.remote"):
            return "origin"
        if cwd == repository_root and args == ("fetch", "origin", "master"):
            return ""
        if cwd == worktree_path and args == ("rebase", "refs/remotes/origin/master"):
            raise RuntimeError("rebase conflict")
        if cwd == worktree_path and args == ("-c", "core.editor=true", "rebase", "--continue"):
            raise RuntimeError("rebase conflict")
        raise AssertionError(f"Unexpected git command: cwd={cwd}, args={args}")

    monkeypatch.setattr(GitAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(GitAdapterService, "_resolved_worktree_path", lambda self: worktree_path)
    monkeypatch.setattr(GitAdapterService, "_git_succeeds", staticmethod(fake_git_succeeds))
    monkeypatch.setattr(GitAdapterService, "_run_git", staticmethod(fake_run_git))

    class FakeRunLogger:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        def __enter__(self):
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            pass

        def started(self, kind: str, *, message: str | None = None, event_id: str | None = None) -> None:
            event_calls.append({"kind": kind, "status": "started", "event_id": event_id, "message": message})

        def success(self, kind: str, *, message: str | None = None, event_id: str | None = None) -> None:
            event_calls.append({"kind": kind, "status": "success", "event_id": event_id, "message": message})

        def failed(self, kind: str, *, message: str | None = None, event_id: str | None = None) -> None:
            event_calls.append({"kind": kind, "status": "failed", "event_id": event_id, "message": message})

    monkeypatch.setattr("common.git_adapter.AgentisRunLogger", FakeRunLogger)
    monkeypatch.setattr(
        "common.git_adapter.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="resolver failed"),
    )

    service = GitAdapterService(
        AgentExecutionContextPayload(
            run_id="run-1",
            task_id="task-1",
            title="Implementace nove funkce",
            project_slug="agentis",
            working_dir="/var/www/repo",
        ),
        make_settings(),
    )

    with pytest.raises(RuntimeError, match="rebase conflict"):
        service.git_merge()

    assert git_calls == [
        (worktree_path, ("branch", "--show-current")),
        (repository_root, ("branch", "--show-current")),
        (repository_root, ("config", "--get", "branch.master.remote")),
        (repository_root, ("fetch", "origin", "master")),
        (worktree_path, ("rebase", "refs/remotes/origin/master")),
        (worktree_path, ("-c", "core.editor=true", "rebase", "--continue")),
    ]
    assert succeeds_calls == [
        (repository_root, ("show-ref", "--verify", "--quiet", "refs/heads/task-1")),
        (worktree_path, ("rev-parse", "--is-inside-work-tree")),
        (repository_root, ("config", "--get", "branch.master.remote")),
        (worktree_path, ("rebase", "--abort")),
        (repository_root, ("checkout", "feature-before-merge")),
    ]
    assert event_calls == [
        {
            "kind": "git-merge-agent",
            "status": "started",
            "event_id": "1",
            "message": "Spouštím git merge AI agenta",
        },
        {
            "kind": "git-merge-agent",
            "status": "success",
            "event_id": "1",
            "message": "",
        },
        {
            "kind": "git merge retry",
            "status": "failed",
            "event_id": "1",
            "message": "rebase conflict",
        },
    ]


def test_git_merge_continues_after_ai_conflict_resolution(monkeypatch):
    repository_root = Path("/var/www/repo")
    worktree_path = Path("/var/www/worktrees/task-1")
    git_calls: list[tuple[Path, tuple[str, ...]]] = []
    event_calls: list[dict[str, Any]] = []

    def fake_git_succeeds(cwd: Path, *args: str) -> bool:
        return (
            (cwd == repository_root and args == ("show-ref", "--verify", "--quiet", "refs/heads/task-1"))
            or (cwd == worktree_path and args == ("rev-parse", "--is-inside-work-tree"))
            or (cwd == repository_root and args == ("config", "--get", "branch.master.remote"))
            or (cwd == repository_root and args == ("checkout", "feature-before-merge"))
        )

    def fake_run_git(cwd: Path, *args: str) -> str:
        git_calls.append((cwd, args))
        if cwd == worktree_path and args == ("branch", "--show-current"):
            return "task-1"
        if cwd == repository_root and args == ("branch", "--show-current"):
            return "feature-before-merge"
        if cwd == repository_root and args == ("config", "--get", "branch.master.remote"):
            return "origin"
        if cwd == repository_root and args == ("fetch", "origin", "master"):
            return ""
        if cwd == worktree_path and args == ("rebase", "refs/remotes/origin/master"):
            raise RuntimeError("rebase conflict")
        if cwd == worktree_path and args == ("-c", "core.editor=true", "rebase", "--continue"):
            return "Successfully rebased and updated refs/heads/task-1."
        if cwd == repository_root and args == ("rebase", "task-1"):
            return ""
        if cwd == repository_root and args == ("rev-parse", "HEAD"):
            return "merge123"
        if cwd == repository_root and args == ("push", "origin", "master:refs/heads/master"):
            return ""
        raise AssertionError(f"Unexpected git command: cwd={cwd}, args={args}")

    monkeypatch.setattr(GitAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(GitAdapterService, "_resolved_worktree_path", lambda self: worktree_path)
    monkeypatch.setattr(GitAdapterService, "_git_succeeds", staticmethod(fake_git_succeeds))
    monkeypatch.setattr(GitAdapterService, "_run_git", staticmethod(fake_run_git))

    class FakeRunLogger:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        def __enter__(self):
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            pass

        def started(self, kind: str, *, message: str | None = None, event_id: str | None = None) -> None:
            event_calls.append({"kind": kind, "status": "started", "event_id": event_id, "message": message})

        def success(self, kind: str, *, message: str | None = None, event_id: str | None = None) -> None:
            event_calls.append({"kind": kind, "status": "success", "event_id": event_id, "message": message})

    monkeypatch.setattr("common.git_adapter.AgentisRunLogger", FakeRunLogger)
    monkeypatch.setattr(
        "common.git_adapter.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="resolved", stderr=""),
    )

    service = GitAdapterService(
        AgentExecutionContextPayload(
            run_id="run-1",
            task_id="task-1",
            title="Implementace nove funkce",
            project_slug="agentis",
            working_dir="/var/www/repo",
        ),
        make_settings(),
    )

    result = service.git_merge()

    assert result["commit"] == "merge123"
    assert result["conflict_resolution_output"] == "resolved"
    assert git_calls == [
        (worktree_path, ("branch", "--show-current")),
        (repository_root, ("branch", "--show-current")),
        (repository_root, ("config", "--get", "branch.master.remote")),
        (repository_root, ("fetch", "origin", "master")),
        (worktree_path, ("rebase", "refs/remotes/origin/master")),
        (worktree_path, ("-c", "core.editor=true", "rebase", "--continue")),
        (repository_root, ("rebase", "task-1")),
        (repository_root, ("rev-parse", "HEAD")),
        (repository_root, ("push", "origin", "master:refs/heads/master")),
    ]
    assert event_calls == [
        {
            "kind": "git-merge-agent",
            "status": "started",
            "event_id": "1",
            "message": "Spouštím git merge AI agenta",
        },
        {
            "kind": "git-merge-agent",
            "status": "success",
            "event_id": "1",
            "message": "resolved",
        },
    ]


def test_git_merge_stashes_unstaged_changes_before_base_rebase(monkeypatch):
    repository_root = Path("/var/www/repo")
    worktree_path = Path("/var/www/worktrees/task-1")
    git_calls: list[tuple[Path, tuple[str, ...]]] = []
    rebase_attempts = 0

    def fake_git_succeeds(cwd: Path, *args: str) -> bool:
        return (
            (cwd == repository_root and args == ("show-ref", "--verify", "--quiet", "refs/heads/task-1"))
            or (cwd == worktree_path and args == ("rev-parse", "--is-inside-work-tree"))
            or (cwd == repository_root and args == ("config", "--get", "branch.master.remote"))
            or (cwd == repository_root and args == ("checkout", "feature-before-merge"))
        )

    def fake_run_git(cwd: Path, *args: str) -> str:
        nonlocal rebase_attempts
        git_calls.append((cwd, args))
        if cwd == worktree_path and args == ("branch", "--show-current"):
            return "task-1"
        if cwd == repository_root and args == ("branch", "--show-current"):
            return "feature-before-merge"
        if cwd == repository_root and args == ("config", "--get", "branch.master.remote"):
            return "origin"
        if cwd == repository_root and args == ("fetch", "origin", "master"):
            return ""
        if cwd == worktree_path and args == ("rebase", "refs/remotes/origin/master"):
            return ""
        if cwd == repository_root and args == ("rebase", "task-1"):
            rebase_attempts += 1
            if rebase_attempts == 1:
                raise RuntimeError("git -C /var/www/repo rebase task-1 failed: You have unstaged changes")
            return ""
        if cwd == repository_root and args == ("stash", "push"):
            return "Saved working directory and index state"
        if cwd == repository_root and args == ("stash", "pop"):
            return ""
        if cwd == repository_root and args == ("rev-parse", "HEAD"):
            return "merge123"
        if cwd == repository_root and args == ("push", "origin", "master:refs/heads/master"):
            return ""
        raise AssertionError(f"Unexpected git command: cwd={cwd}, args={args}")

    monkeypatch.setattr(GitAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(GitAdapterService, "_resolved_worktree_path", lambda self: worktree_path)
    monkeypatch.setattr(GitAdapterService, "_git_succeeds", staticmethod(fake_git_succeeds))
    monkeypatch.setattr(GitAdapterService, "_run_git", staticmethod(fake_run_git))

    service = GitAdapterService(
        AgentExecutionContextPayload(
            run_id="run-1",
            task_id="task-1",
            title="Implementace nove funkce",
            project_slug="agentis",
            working_dir="/var/www/repo",
        ),
        make_settings(),
    )

    result = service.git_merge()

    assert result["commit"] == "merge123"
    assert git_calls == [
        (worktree_path, ("branch", "--show-current")),
        (repository_root, ("branch", "--show-current")),
        (repository_root, ("config", "--get", "branch.master.remote")),
        (repository_root, ("fetch", "origin", "master")),
        (worktree_path, ("rebase", "refs/remotes/origin/master")),
        (repository_root, ("rebase", "task-1")),
        (repository_root, ("stash", "push")),
        (repository_root, ("rebase", "task-1")),
        (repository_root, ("stash", "pop")),
        (repository_root, ("rev-parse", "HEAD")),
        (repository_root, ("push", "origin", "master:refs/heads/master")),
    ]


def test_git_merge_fails_when_local_base_cannot_fast_forward_to_remote(monkeypatch):
    repository_root = Path("/var/www/repo")
    worktree_path = Path("/var/www/worktrees/task-1")
    git_calls: list[tuple[str, ...]] = []
    succeeds_calls: list[tuple[Path, tuple[str, ...]]] = []

    def fake_git_succeeds(cwd: Path, *args: str) -> bool:
        succeeds_calls.append((cwd, args))
        return (
            (cwd == repository_root and args == ("show-ref", "--verify", "--quiet", "refs/heads/task-1"))
            or (cwd == worktree_path and args == ("rev-parse", "--is-inside-work-tree"))
            or (cwd == repository_root and args == ("config", "--get", "branch.master.remote"))
            or (cwd == repository_root and args == ("checkout", "feature-before-merge"))
        )

    def fake_run_git(cwd: Path, *args: str) -> str:
        git_calls.append(args)
        if cwd == worktree_path and args == ("branch", "--show-current"):
            return "task-1"
        if cwd == repository_root and args == ("branch", "--show-current"):
            return "feature-before-merge"
        if cwd == repository_root and args == ("config", "--get", "branch.master.remote"):
            return "origin"
        if cwd == repository_root and args == ("fetch", "origin", "master"):
            return ""
        if cwd == worktree_path and args == ("rebase", "refs/remotes/origin/master"):
            return ""
        if cwd == repository_root and args == ("rebase", "task-1"):
            raise RuntimeError("Not possible to fast-forward")
        raise AssertionError(f"Unexpected git command: cwd={cwd}, args={args}")

    monkeypatch.setattr(GitAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(GitAdapterService, "_resolved_worktree_path", lambda self: worktree_path)
    monkeypatch.setattr(GitAdapterService, "_git_succeeds", staticmethod(fake_git_succeeds))
    monkeypatch.setattr(GitAdapterService, "_run_git", staticmethod(fake_run_git))

    service = GitAdapterService(
        AgentExecutionContextPayload(
            run_id="run-1",
            task_id="task-1",
            title="Implementace nove funkce",
            project_slug="agentis",
            working_dir="/var/www/repo",
        ),
        make_settings(),
    )

    with pytest.raises(RuntimeError, match="Not possible to fast-forward"):
        service.git_merge()

    assert git_calls == [
        ("branch", "--show-current"),
        ("branch", "--show-current"),
        ("config", "--get", "branch.master.remote"),
        ("fetch", "origin", "master"),
        ("rebase", "refs/remotes/origin/master"),
        ("rebase", "task-1"),
    ]
    assert succeeds_calls == [
        (repository_root, ("show-ref", "--verify", "--quiet", "refs/heads/task-1")),
        (worktree_path, ("rev-parse", "--is-inside-work-tree")),
        (repository_root, ("config", "--get", "branch.master.remote")),
        (repository_root, ("checkout", "feature-before-merge")),
    ]


def test_start_rejects_path_inside_adapter_manifest():
    client = make_client()

    response = client.post(
        "/api",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "start",
            "params": {
                "context": {
                    **make_start_params()["context"],
                    "adapter": {
                        "agent": "build",
                        "model": "gpt-5.4",
                        "manifest": "nested/opencode.yaml",
                    },
                }
            },
        },
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == -32602
    assert any(item["loc"] == ["context", "adapter", "manifest"] for item in payload["error"]["data"])
