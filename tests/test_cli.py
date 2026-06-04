from __future__ import annotations

from typing import Any

from app.cli import run


def test_cli_runs_websocket_transport_without_http_listener(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_run_passive_websocket(*, settings: Any, dispatch: Any, service_container: Any) -> None:
        captured["settings"] = settings
        captured["dispatch"] = dispatch
        captured["service_container"] = service_container

    monkeypatch.setenv("AGENTIS_WS_ENDPOINT", "ws://127.0.0.1:8892/api/adapters/passive/ws")
    monkeypatch.setenv("AGENTIS_ADAPTER_ID", "adapter-1")
    monkeypatch.setenv("AGENTIS_TOKEN", "secret")
    monkeypatch.setattr("app.cli.run_passive_websocket", fake_run_passive_websocket)

    run(["--adapter", "opencode", "--id", "opencode"])

    assert captured["settings"].agentis_adapter_id == "opencode"
    assert "start" in captured["dispatch"]
    # The service container is the FastAPI app state holding the configured services.
    assert captured["service_container"].agent_jsonrpc_service is not None


def test_cli_accepts_agentiscode_adapter(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_run_passive_websocket(*, settings: Any, dispatch: Any, service_container: Any) -> None:
        captured["settings"] = settings
        captured["dispatch"] = dispatch
        captured["service_container"] = service_container

    monkeypatch.setenv("AGENTIS_WS_ENDPOINT", "ws://127.0.0.1:8892/api/adapters/passive/ws")
    monkeypatch.setenv("AGENTIS_ADAPTER_ID", "adapter-1")
    monkeypatch.setenv("AGENTIS_TOKEN", "secret")
    monkeypatch.setattr("app.cli.run_passive_websocket", fake_run_passive_websocket)

    run(["--adapter", "agentiscode", "--id", "agentiscode"])

    assert captured["settings"].agentis_adapter_id == "agentiscode"
    assert "start" in captured["dispatch"]
    assert captured["service_container"].agent_jsonrpc_service is not None
