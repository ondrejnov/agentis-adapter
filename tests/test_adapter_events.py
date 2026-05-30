from pathlib import Path
from typing import Any

from common.config import Settings
from common.models import AgentExecutionContextPayload
from common.kubernetes_runtime import KubernetesAdapterService


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
    manifest = tmp_path / "manifest"
    manifest.mkdir()
    return Settings(
        host="0.0.0.0",
        port=8001,
        default_namespace="agentis",
        app_host=None,
        manifest_path=manifest,
        worktree_root=worktree,
        public_base_url="http://adapter.local",
        agentis_endpoint="http://agentis.local",
        agentis_token="token",
    )


def test_post_agentis_event_invokes_rpc_without_log_kwarg_conflict(tmp_path, monkeypatch):
    """Regression: log_json(level, message, **fields) conflicts with fields named 'message'."""
    service = KubernetesAdapterService(_make_context(), _make_settings(tmp_path))
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


def test_close_falls_back_when_git_worktree_remove_fails(monkeypatch, tmp_path):
    service = KubernetesAdapterService(_make_context(), _make_settings(tmp_path))
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    worktree_path = tmp_path / "worktree" / "task-1"
    worktree_path.mkdir(parents=True)
    (worktree_path / "untracked.txt").write_text("dirty", encoding="utf-8")

    captured: list[tuple[Path, tuple[str, ...]]] = []

    class FakeManifestParser:
        def __init__(self, **_: Any) -> None:
            pass

        def delete(self, source_path: str, ignore_not_found: bool = True) -> str:
            return "deleted"

    def fake_run_git(cwd: Path, *args: str) -> str:
        captured.append((cwd, args))
        if args == ("worktree", "remove", "--force", str(worktree_path)):
            raise RuntimeError("git worktree remove failed: Directory not empty")
        if args == ("branch", "-D", "task-1"):
            return "Deleted branch task-1 (was abc123)."
        raise AssertionError(f"Unexpected git command: {args}")

    monkeypatch.setattr("common.kubernetes_runtime.OpenCodeManifestParser", FakeManifestParser)
    monkeypatch.setattr(KubernetesAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(KubernetesAdapterService, "_resolved_worktree_path", lambda self: worktree_path)
    monkeypatch.setattr(
        KubernetesAdapterService,
        "_resolve_manifest_source",
        lambda self: tmp_path / "opencode.yaml",
    )
    monkeypatch.setattr(
        KubernetesAdapterService,
        "_git_succeeds",
        staticmethod(
            lambda cwd, *args: (
                args == ("show-ref", "--verify", "--quiet", "refs/heads/task-1") or args == ("worktree", "prune")
            )
        ),
    )
    monkeypatch.setattr(KubernetesAdapterService, "_run_git", staticmethod(fake_run_git))

    result = service.close()

    assert result["worktree_removed"] is True
    assert result["branch_deleted"] is True
    assert not worktree_path.exists()
    assert captured == [
        (repository_root, ("worktree", "remove", "--force", str(worktree_path))),
        (repository_root, ("branch", "-D", "task-1")),
    ]


def test_create_worktree_preserves_permissions(monkeypatch, tmp_path):
    service = KubernetesAdapterService(_make_context(), _make_settings(tmp_path))
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

    monkeypatch.setattr(KubernetesAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(KubernetesAdapterService, "_resolve_base_ref", lambda self, root: "master")
    monkeypatch.setattr(
        KubernetesAdapterService,
        "_git_succeeds",
        staticmethod(lambda cwd, *args: False),
    )
    monkeypatch.setattr(KubernetesAdapterService, "_run_git", staticmethod(fake_run_git))

    result = service.create_worktree()

    assert result["status"] == "created"
    assert worktree_root.stat().st_mode & 0o777 == 0o755
    assert worktree_path.stat().st_mode & 0o777 == 0o700
    assert (worktree_path / "tracked.txt").stat().st_mode & 0o777 == 0o600
    assert (worktree_path / "nested").stat().st_mode & 0o777 == 0o700


def test_git_merge_rebases_task_branch_before_fast_forwarding_base(monkeypatch, tmp_path):
    service = KubernetesAdapterService(_make_context(), _make_settings(tmp_path))
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    worktree_path = tmp_path / "worktree" / "task-1"
    worktree_path.mkdir(parents=True)

    captured: list[tuple[Path, tuple[str, ...]]] = []
    succeeds_calls: list[tuple[Path, tuple[str, ...]]] = []

    def fake_run_git(cwd: Path, *args: str) -> str:
        captured.append((cwd, args))
        if cwd == worktree_path and args == ("branch", "--show-current"):
            return "task-1"
        if cwd == repository_root and args == ("branch", "--show-current"):
            return "other-branch"
        if cwd == repository_root and args == ("fetch", "origin", "master"):
            return ""
        if cwd == worktree_path and args == ("rebase", "refs/remotes/origin/master"):
            return "Successfully rebased and updated refs/heads/task-1."
        if cwd == repository_root and args == ("rebase", "task-1"):
            return "Updating def456..abc123"
        if cwd == repository_root and args == ("rev-parse", "HEAD"):
            return "abc123"
        if cwd == repository_root and args == ("push", "origin", "master:refs/heads/master"):
            return ""
        if cwd == repository_root and args == ("checkout", "other-branch"):
            return ""
        raise AssertionError(f"Unexpected git command: cwd={cwd}, args={args}")

    monkeypatch.setattr(KubernetesAdapterService, "_repository_root", lambda self: repository_root)
    monkeypatch.setattr(KubernetesAdapterService, "_resolved_worktree_path", lambda self: worktree_path)
    monkeypatch.setattr(KubernetesAdapterService, "_resolve_base_ref", lambda self, root: "master")
    monkeypatch.setattr(KubernetesAdapterService, "_resolve_push_remote", lambda self, root: "origin")

    def fake_git_succeeds(cwd: Path, *args: str) -> bool:
        succeeds_calls.append((cwd, args))
        return (
            (cwd == repository_root and args == ("show-ref", "--verify", "--quiet", "refs/heads/task-1"))
            or (cwd == worktree_path and args == ("rev-parse", "--is-inside-work-tree"))
            or (cwd == repository_root and args == ("checkout", "other-branch"))
        )

    monkeypatch.setattr(
        KubernetesAdapterService,
        "_git_succeeds",
        staticmethod(fake_git_succeeds),
    )
    monkeypatch.setattr(KubernetesAdapterService, "_run_git", staticmethod(fake_run_git))

    result = service.git_merge()

    assert result == {
        "action": "git_merge",
        "task_id": "task-1",
        "branch": "task-1",
        "base_branch": "master",
        "merge_commit": "abc123",
        "commit": "abc123",
        "push_remote": "origin",
        "repository_root": str(repository_root),
    }
    assert captured == [
        (worktree_path, ("branch", "--show-current")),
        (repository_root, ("branch", "--show-current")),
        (repository_root, ("fetch", "origin", "master")),
        (worktree_path, ("rebase", "refs/remotes/origin/master")),
        (repository_root, ("rebase", "task-1")),
        (repository_root, ("rev-parse", "HEAD")),
        (repository_root, ("push", "origin", "master:refs/heads/master")),
    ]
    assert succeeds_calls == [
        (repository_root, ("show-ref", "--verify", "--quiet", "refs/heads/task-1")),
        (worktree_path, ("rev-parse", "--is-inside-work-tree")),
        (repository_root, ("checkout", "other-branch")),
    ]
