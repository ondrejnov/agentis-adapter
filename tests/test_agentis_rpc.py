from __future__ import annotations

from typing import Any, cast

import pytest

from common.agentis import AUTH_HEADER, AgentisJsonRpcClient, AgentisJsonRpcError, AgentisRunLogger


class FakeResponse:
    def __init__(self, body: Any, status_code: int = 200, text: str = "") -> None:
        self.body = body
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        if isinstance(self.body, BaseException):
            raise self.body
        return self.body


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.headers: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self.response

    def close(self) -> None:
        self.closed = True


def test_agentis_jsonrpc_client_posts_normalized_authenticated_request() -> None:
    session = FakeSession(FakeResponse({"jsonrpc": "2.0", "id": "req-1", "result": {"ok": True}}))
    client = AgentisJsonRpcClient("http://agentis.local/", token="secret", timeout=12.0, session=cast(Any, session))

    result = client.call("run.adapter_event", {"kind": "deploy"}, request_id="req-1")

    assert result == {"ok": True}
    assert client.endpoint == "http://agentis.local/api"
    assert session.headers[AUTH_HEADER] == "secret"
    assert session.calls == [
        {
            "url": "http://agentis.local/api",
            "json": {
                "jsonrpc": "2.0",
                "id": "req-1",
                "method": "run.adapter_event",
                "params": {"kind": "deploy"},
            },
            "timeout": 12.0,
        }
    ]


def test_agentis_jsonrpc_client_raises_shared_error_for_rpc_error() -> None:
    error = {"code": -32000, "message": "boom"}
    session = FakeSession(FakeResponse({"jsonrpc": "2.0", "id": "req-1", "error": error}))
    client = AgentisJsonRpcClient("http://agentis.local/api", session=cast(Any, session))

    with pytest.raises(AgentisJsonRpcError) as exc_info:
        client.call("run.adapter_event", request_id="req-1")

    assert str(exc_info.value) == "boom"
    assert exc_info.value.status_code == 200
    assert exc_info.value.details == error


def test_agentis_run_logger_posts_system_event() -> None:
    session = FakeSession(FakeResponse({"jsonrpc": "2.0", "id": "req-1", "result": {"ok": True}}))
    client = AgentisJsonRpcClient("http://agentis.local", session=cast(Any, session))
    logger = AgentisRunLogger(" run-1 ", client=client, timeout=7.0)

    result = logger.success(message="Hotovo", data={"step": "deploy"}, event_id="system:1")

    assert result == {"ok": True}
    assert session.calls == [
        {
            "url": "http://agentis.local/api",
            "json": {
                "jsonrpc": "2.0",
                "id": "agentis-run-log-run-1-system:1-success",
                "method": "run.adapter_event",
                "params": {
                    "run_id": "run-1",
                    "kind": "system",
                    "status": "success",
                    "event_id": "system:1",
                    "message": "Hotovo",
                    "data": {"step": "deploy"},
                },
            },
            "timeout": 7.0,
        }
    ]


def test_agentis_run_logger_requires_non_empty_values() -> None:
    session = FakeSession(FakeResponse({"jsonrpc": "2.0", "id": "req-1", "result": {"ok": True}}))
    client = AgentisJsonRpcClient("http://agentis.local", session=cast(Any, session))

    with pytest.raises(ValueError, match="run_id"):
        AgentisRunLogger(" ", client=client)

    logger = AgentisRunLogger("run-1", client=client)
    with pytest.raises(ValueError, match="kind"):
        logger.started(" ")
