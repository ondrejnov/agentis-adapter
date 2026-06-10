from pathlib import Path

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.git_adapter import GitAdapterService


def _make_context() -> AgentExecutionContextPayload:
    return AgentExecutionContextPayload(
        task_id="task-1",
        run_id="019db180-0cf0-71eb-af2b-051b7d683dd5",
        title="example",
        project_slug="proj",
        session_id=None,
    )


def _make_settings(tmp_path: Path) -> Settings:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return Settings(
        host="0.0.0.0",
        port=8001,
        worktree_root=worktree,
        public_base_url="http://adapter.local",
        agentis_endpoint="http://agentis.local",
        agentis_token="token",
    )


def test_post_agentis_event_invokes_rpc_without_log_kwarg_conflict(tmp_path, monkeypatch):
    """Regression: log_json(level, message, **fields) conflicts with fields named 'message'."""
    service = GitAdapterService(_make_context(), _make_settings(tmp_path))
    captured: dict = {}

    def fake_call(method: str, payload: dict):  # noqa: ANN001
        captured["method"] = method
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(service, "_call_agentis_rpc", fake_call)

    # Must not raise — previously raised TypeError because log_json got two 'message' args.
    service.post_agentis_event(kind="deploy", status="started", message="hi", data={"k": "v"})

    assert captured["method"] == "run.adapter_event"
    event_id = captured["payload"].pop("event_id")
    assert isinstance(event_id, str)
    assert event_id.startswith("deploy:")
    assert captured["payload"] == {
        "run_id": "019db180-0cf0-71eb-af2b-051b7d683dd5",
        "kind": "deploy",
        "status": "started",
        "message": "hi",
        "data": {"k": "v"},
    }


def test_create_worktree_preserves_permissions(monkeypatch, tmp_path):
    service = GitAdapterService(_make_context(), _make_settings(tmp_path))
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    worktree_root = tmp_path / "worktree"
    worktree_path = worktree_root / "task-1"

    def fake_run_git(cwd: Path, *args: str) -> str:
        if cwd == repository_root and args == ("worktree", "add", "-b", "task-1", str(worktree_path), "master"):
            worktree_path.mkdir(parents=True, exist_ok=True)
            worktree_path.chmod(0o700)
            nested = worktree_path / "tracked.txt"
            nested.write_text("content", encoding="utf-8")
            nested.chmod(0o600)
            nested_dir = worktree_path / "nested"
            nested_dir.mkdir()
            nested_dir.chmod(0o700)
            return ""
        raise AssertionError(f"Unexpected git command: cwd={cwd}, args={args}")

    monkeypatch.setattr(GitAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(GitAdapterService, "_resolve_base_ref", lambda self, root: "master")
    monkeypatch.setattr(
        GitAdapterService,
        "_git_succeeds",
        staticmethod(lambda cwd, *args: False),
    )
    monkeypatch.setattr(GitAdapterService, "_run_git", staticmethod(fake_run_git))

    result = service.create_worktree()

    assert result["status"] == "created"
    assert worktree_root.stat().st_mode & 0o777 == 0o755
    assert worktree_path.stat().st_mode & 0o777 == 0o700
    assert (worktree_path / "tracked.txt").stat().st_mode & 0o777 == 0o600
    assert (worktree_path / "nested").stat().st_mode & 0o777 == 0o700
