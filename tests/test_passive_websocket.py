from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from common.config import Settings
from common.models import ApproveParams
from common.rpc.dispatcher import JsonRpcRoute
from common.rpc.passive_websocket import PassiveWebSocketClient


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8001,
        "default_namespace": "agentis",
        "app_host": None,
        "manifest_path": Path("/tmp/opencode.yaml"),
        "worktree_root": Path("/var/www/worktrees"),
        "public_base_url": "http://adapter.internal:8001",
        "agentis_endpoint": "http://agentis.local",
        "agentis_token": "super-secret-token",
        "adapter_transport": "websocket",
        "agentis_ws_endpoint": "ws://127.0.0.1:8891/ws/adapters",
        "agentis_adapter_id": "adapter-1",
    }
    values.update(overrides)
    return Settings(**values)


def test_passive_websocket_dispatch_preserves_response_id():
    class FakeService:
        def approve(self, params: ApproveParams) -> dict[str, Any]:
            return {"approved": params.approved}

    client = PassiveWebSocketClient(
        settings=make_settings(),
        dispatch={"approve": JsonRpcRoute(ApproveParams, "approve")},
        service_container=SimpleNamespace(agent_jsonrpc_service=FakeService()),
    )

    response = asyncio.run(
        client.dispatch_message(
            '{"jsonrpc":"2.0","id":"run-123:start","method":"approve","params":{"run_id":"run-1","approved":true}}'
        )
    )

    assert response == {"jsonrpc": "2.0", "id": "run-123:start", "result": {"approved": True}}


def test_passive_websocket_invalid_json_returns_parse_error():
    client = PassiveWebSocketClient(settings=make_settings(), dispatch={}, service_container=SimpleNamespace())

    response = asyncio.run(client.dispatch_message("{"))

    assert response is not None
    assert response["id"] is None
    assert response["error"]["code"] == -32700


def test_passive_websocket_connect_uses_configured_max_message_size(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeConnection:
        async def __aenter__(self) -> FakeConnection:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def __aiter__(self) -> FakeConnection:
            return self

        async def __anext__(self) -> str:
            raise StopAsyncIteration

    def fake_connect(*args: Any, **kwargs: Any) -> FakeConnection:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeConnection()

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=fake_connect))
    settings = make_settings(websocket_max_message_size=123456)
    client = PassiveWebSocketClient(settings=settings, dispatch={}, service_container=SimpleNamespace())

    asyncio.run(client._run_once())

    assert captured["kwargs"]["max_size"] == 123456


def test_passive_websocket_rejects_insecure_non_local_endpoint():
    with pytest.raises(ValueError, match="wss://"):
        make_settings(agentis_ws_endpoint="ws://agentis.example/ws").validate_passive_websocket()


def test_passive_websocket_reconnect_log_omits_token(monkeypatch, caplog):
    settings = make_settings(websocket_reconnect_initial_delay=0, websocket_reconnect_max_attempts=1)
    client = PassiveWebSocketClient(settings=settings, dispatch={}, service_container=SimpleNamespace())

    async def fail_once() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_run_once", fail_once)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    with caplog.at_level("WARNING"):
        asyncio.run(client.run_forever())

    assert "adapter-1" in caplog.text
    assert "super-secret-token" not in caplog.text


def test_passive_websocket_reconnect_log_includes_status(monkeypatch, caplog):
    settings = make_settings(websocket_reconnect_initial_delay=0, websocket_reconnect_max_attempts=1)
    client = PassiveWebSocketClient(settings=settings, dispatch={}, service_container=SimpleNamespace())

    class FakeResponse:
        status_code = 400
        reason_phrase = "Bad Request"

    class FakeInvalidStatus(Exception):
        response = FakeResponse()

    async def fail_once() -> None:
        raise FakeInvalidStatus()

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(client, "_run_once", fail_once)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    with caplog.at_level("WARNING"):
        asyncio.run(client.run_forever())

    assert "status=400" in caplog.text
    assert "Bad Request" in caplog.text
    assert "super-secret-token" not in caplog.text
