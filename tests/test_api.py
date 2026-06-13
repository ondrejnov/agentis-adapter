from pathlib import Path
from typing import cast
from typing import Any


from common.config import Settings
from app.adapter_api import create_app, _DISPATCH
from common.models import (
    AgentExecutionContextPayload,
    AdapterOptionsPayload,
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


class _StubWorkflowManager:
    """Workflow manager stub — start jen ověří, že run doběhne k workflow dispatchi."""

    def start_workflow(self, context: Any, worktree: str, prompt: str) -> dict[str, str]:
        return {"action": "workflow_start", "status": "applied"}


def _workflow_service(worktree: Path) -> AgentJsonRpcService:
    class FakeAdapter:
        def create_worktree(self) -> dict[str, str]:
            return {"action": "create_worktree", "status": "created", "working_dir": str(worktree)}

    return AgentJsonRpcService(
        settings=make_settings(),
        adapter_factory=cast(Any, lambda context: FakeAdapter()),
        workflow_manager=cast(Any, _StubWorkflowManager()),
    )


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


def _followup_actions(worktree: Path) -> list[dict[str, Any]]:
    from common.workflow.schema import WORKFLOW_FILE_RELPATH, load_workflow_followups

    return [followup.to_action() for followup in load_workflow_followups(worktree / WORKFLOW_FILE_RELPATH)]


def test_completion_actions_dispatch_named_workflows_via_start(tmp_path):
    workflow_path = tmp_path / ".agentis" / "workflows" / "default.yaml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(
        "version: 1\n"
        "workflow:\n"
        "  followups:\n"
        "    - title: Git merge\n"
        "      prompt: Sloučit změny z task větve do hlavní větve.\n"
        "      workflow: merge\n"
        "    - title: Zavřít prostředí\n"
        "      prompt: Uklidit prostředí, worktree a task větev.\n"
        "      workflow: close\n"
        "  image: registry.example/agent:1.0\n"
        "  steps:\n"
        "    - name: Run agent\n"
        "      run: agentiscode\n",
        encoding="utf-8",
    )

    actions = _followup_actions(tmp_path)

    # Followup akce nejsou samostatné RPC metody — dispatchují `start` s názvem workflow v kontextu.
    # Nabídka se konfiguruje v `workflow.followups` sekci workflow YAML ve worktree.
    assert [(action["adapter_method"], action["workflow"]) for action in actions] == [
        ("start", "merge"),
        ("start", "close"),
    ]
    assert all(action["continue_previous_run"] is False for action in actions)


def test_completion_actions_without_followups_section_offer_nothing(tmp_path):
    # bez workflow souboru se žádné akce nenabízí
    assert _followup_actions(tmp_path) == []

    workflow_path = tmp_path / ".agentis" / "workflows" / "default.yaml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(
        "version: 1\nworkflow:\n  steps:\n    - name: Run agent\n      run: agentiscode\n",
        encoding="utf-8",
    )
    assert _followup_actions(tmp_path) == []


def strip_event_id(params: dict[str, Any]) -> dict[str, Any]:
    result = dict(params)
    event_id = result.pop("event_id", None)
    assert isinstance(event_id, str)
    return result


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


def test_start_accepts_extended_agent_execution_context_schema(tmp_path: Path):
    service = _workflow_service(tmp_path)
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


def test_start_accepts_null_for_defaulted_context_fields(tmp_path: Path):
    service = _workflow_service(tmp_path)
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
