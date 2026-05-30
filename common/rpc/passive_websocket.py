from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

from common.config import Settings
from common.rpc.dispatcher import JsonRpcRoute, dispatch_jsonrpc_payload, error_response


logger = logging.getLogger(__name__)


class PassiveWebSocketClient:
    def __init__(self, *, settings: Settings, dispatch: Mapping[str, JsonRpcRoute], service_container: Any) -> None:
        settings.validate_passive_websocket()
        self.settings = settings
        self.dispatch = dispatch
        self.service_container = service_container

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.agentis_token}",
            "X-Agentis-Adapter-Id": self.settings.agentis_adapter_id or "",
        }

    @staticmethod
    def _error_summary(exc: Exception) -> str:
        parts = [exc.__class__.__name__]
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code is None:
            status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            parts.append(f"status={status_code}")
        reason = getattr(response, "reason_phrase", None) or getattr(exc, "reason", None)
        if reason:
            parts.append(f"reason={reason}")
        return " ".join(parts)

    async def run_forever(self) -> None:
        attempts = 0
        delay = self.settings.websocket_reconnect_initial_delay
        while self.settings.websocket_reconnect_max_attempts == 0 or attempts < self.settings.websocket_reconnect_max_attempts:
            attempts += 1
            try:
                await self._run_once()
                attempts = 0
                delay = self.settings.websocket_reconnect_initial_delay
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Passive WebSocket disconnected adapter_id=%s error=%s",
                    self.settings.agentis_adapter_id,
                    self._error_summary(exc),
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.settings.websocket_reconnect_max_delay)

    async def _run_once(self) -> None:
        import websockets

        assert self.settings.agentis_ws_endpoint is not None
        async with websockets.connect(
            self.settings.agentis_ws_endpoint,
            additional_headers=self._headers(),
            ping_interval=self.settings.websocket_heartbeat_interval,
            max_size=self.settings.websocket_max_message_size,
        ) as websocket:
            logger.info("Passive WebSocket connected adapter_id=%s", self.settings.agentis_adapter_id)
            async for raw_message in websocket:
                response = await self.dispatch_message(raw_message)
                if response is not None:
                    await websocket.send(json.dumps(response))

    async def dispatch_message(self, raw_message: str | bytes) -> dict[str, Any] | None:
        request_id = None
        try:
            payload = json.loads(raw_message)
        except Exception as exc:
            return error_response(None, -32700, f"Parse error: {exc}")

        if isinstance(payload, dict):
            request_id = payload.get("id")
        result = await dispatch_jsonrpc_payload(payload, self.dispatch, self.service_container)
        if request_id is None:
            return None
        return result.body


async def run_passive_websocket(
    *, settings: Settings, dispatch: Mapping[str, JsonRpcRoute], service_container: Any
) -> None:
    await PassiveWebSocketClient(settings=settings, dispatch=dispatch, service_container=service_container).run_forever()
