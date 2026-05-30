"""Test helpers for the adapter WebSocket transport.

External Agentis JSON-RPC (``start``, ``add_message`` …) is not served over HTTP; in
production it arrives over the passive WebSocket transport. ``RpcTestClient`` lets
existing tests keep calling ``client.post("/api", json=...)`` by routing those payloads
through the same dispatcher the WebSocket client uses, while ``/health`` still goes
through a real ``TestClient``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.rpc.dispatcher import JsonRpcRoute, dispatch_jsonrpc_payload


class _DispatchResponse:
    """Minimal stand-in for an HTTP response returned by ``RpcTestClient``."""

    def __init__(self, body: dict[str, Any], status_code: int) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body, ensure_ascii=False)


class RpcTestClient:
    """TestClient wrapper that dispatches external ``/api`` JSON-RPC in-process."""

    def __init__(self, app: FastAPI, external_dispatch: Mapping[str, JsonRpcRoute]) -> None:
        self.app = app
        self._external_dispatch = external_dispatch
        self._http = TestClient(app)

    def post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        if path == "/api":
            result = asyncio.run(dispatch_jsonrpc_payload(json, self._external_dispatch, self.app.state))
            return _DispatchResponse(result.body, result.http_status)
        return self._http.post(path, json=json, **kwargs)

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._http.get(path, **kwargs)
