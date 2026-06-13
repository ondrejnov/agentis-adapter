from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from common.config import Settings
from common.shutdown import drain_running_work
from common.workflow.manager import WorkflowManager, _WorkflowRun


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8001,
        "worktree_root": Path("/var/www/worktrees"),
        "public_base_url": None,
        "agentis_endpoint": None,
        "agentis_token": None,
    }
    values.update(overrides)
    return Settings(**values)


def _running_thread(release: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    return thread


def test_workflow_manager_wait_idle_waits_for_running_threads():
    manager = WorkflowManager(settings=make_settings(), runner=SimpleNamespace())
    release = threading.Event()
    run = _WorkflowRun(
        context=None,
        worktree=Path("/tmp"),
        workflow=SimpleNamespace(),
        namespace="ns",
        attempt_id="a1",
        run_dir=Path("/tmp"),
        output_root=Path("/tmp"),
        prompt_file=Path("/tmp/prompt.md"),
        context_file=Path("/tmp/context.json"),
        executor="local",
        runner=SimpleNamespace(),
    )
    run.thread = _running_thread(release)
    manager._runs["task-1"] = run

    assert manager.active_count() == 1
    assert manager.wait_idle(timeout=0.05) is False

    release.set()
    assert manager.wait_idle(timeout=5) is True
    assert manager.active_count() == 0


class _FakeWaitable:
    def __init__(self, *, finishes: bool) -> None:
        self._finishes = finishes
        self.waited_with: list[float | None] = []

    def active_count(self) -> int:
        return 0 if self._finishes and self.waited_with else 1

    def wait_idle(self, timeout: float | None = None) -> bool:
        self.waited_with.append(timeout)
        return self._finishes


def test_drain_running_work_waits_for_all_waitable_services():
    busy = _FakeWaitable(finishes=True)
    container = SimpleNamespace(
        agent_jsonrpc_service=busy,
        claude_session_manager=_FakeWaitable(finishes=True),
        unrelated="ignore-me",
    )

    assert drain_running_work(container, timeout=0) is True
    assert busy.waited_with == [None]  # timeout 0 = bez limitu


def test_drain_running_work_reports_timeout():
    container = SimpleNamespace(stuck=_FakeWaitable(finishes=False))

    assert drain_running_work(container, timeout=0.05) is False
    assert container.stuck.waited_with and container.stuck.waited_with[0] is not None


def test_drain_running_work_reads_fastapi_state_container():
    class FakeState:
        def __init__(self) -> None:
            self._state = {"manager": _FakeWaitable(finishes=True)}

    container = FakeState()
    assert drain_running_work(container, timeout=0) is True
    assert container._state["manager"].waited_with == [None]


def test_jsonrpc_service_wait_idle_without_workflow_manager():
    from common.rpc.jsonrpc import AgentJsonRpcService

    service = AgentJsonRpcService(settings=make_settings(), adapter_factory=lambda context: None)

    assert service.active_count() == 0
    assert service.wait_idle(0.01) is True
