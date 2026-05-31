"""Agentis task gateway for the Slack listener.

Thin wrapper over :class:`common.agentis.AgentisJsonRpcClient` exposing just the
task-lifecycle methods the listener needs. The ``claude``/``opencode`` adapters
only ever *report* progress on an existing run; the Slack source additionally
*creates* tasks and *starts* runs, hence this dedicated gateway.
"""

from __future__ import annotations

from typing import Any

from common.agentis import AgentisJsonRpcClient


class SlackAgentisGateway:
    def __init__(self, *, endpoint: str, token: str | None, timeout: float = 60.0) -> None:
        self._client = AgentisJsonRpcClient(endpoint=endpoint, token=token, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def find_by_external_ref(self, filters: dict[str, str]) -> dict | None:
        result = self._client.call("task.find_by_external_ref", {"filters": filters})
        if isinstance(result, dict):
            item = result.get("item")
            return item if isinstance(item, dict) else None
        return None

    def save_task(self, data: dict[str, Any]) -> dict:
        return self._client.call("task.save", {"data": data})

    def start_run(self, task_id: str, *, start_adapter: bool = True) -> Any:
        return self._client.call("task.start_run", {"id": task_id, "start_adapter": start_adapter})

    def question_reply(self, external_id: str, results: list[dict]) -> Any:
        return self._client.call("task.question_reply", {"external_id": external_id, "results": results})
