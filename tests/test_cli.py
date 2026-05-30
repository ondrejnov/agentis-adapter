from __future__ import annotations

import asyncio
from typing import Any

from app.cli import run


def test_cli_runs_websocket_transport_with_internal_http_listener(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeServer:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.should_exit = False
            captured["server"] = self
            captured["config"] = config

        async def serve(self) -> None:
            captured["serve_started"] = True
            while not self.should_exit:
                await asyncio.sleep(0)
            captured["serve_stopped"] = True

    async def fake_run_passive_websocket(*, settings: Any, dispatch: Any, service_container: Any) -> None:
        captured["settings"] = settings
        captured["dispatch"] = dispatch
        captured["service_container"] = service_container

    monkeypatch.setenv("AGENTIS_WS_ENDPOINT", "ws://127.0.0.1:8892/api/adapters/passive/ws")
    monkeypatch.setenv("AGENTIS_ADAPTER_ID", "adapter-1")
    monkeypatch.setenv("AGENTIS_TOKEN", "secret")
    monkeypatch.setattr("app.cli.uvicorn.Server", FakeServer)
    monkeypatch.setattr("app.cli.run_passive_websocket", fake_run_passive_websocket)

    run([
        "--adapter",
        "opencode",
        "--host",
        "127.0.0.1",
        "--port",
        "8101",
        "--id",
        "opencode",
    ])

    assert captured["config"].host == "127.0.0.1"
    assert captured["config"].port == 8101
    assert captured["config"].app.state is captured["service_container"]
    assert captured["settings"].agentis_adapter_id == "opencode"
    assert "start" in captured["dispatch"]
    assert captured["serve_started"] is True
    assert captured["serve_stopped"] is True
