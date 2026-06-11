from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import Mapping
from typing import Any

from common.config import Settings
from common.rpc.dispatcher import JsonRpcRoute, dispatch_jsonrpc_payload, error_response
from common.shutdown import drain_running_work


logger = logging.getLogger(__name__)


class PassiveWebSocketClient:
    def __init__(self, *, settings: Settings, dispatch: Mapping[str, JsonRpcRoute], service_container: Any) -> None:
        settings.validate_passive_websocket()
        self.settings = settings
        self.dispatch = dispatch
        self.service_container = service_container
        self._shutdown_event = asyncio.Event()

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

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    def request_shutdown(self, reason: str | None = None) -> None:
        """Požádá o graceful shutdown: přestat přijímat zprávy a zavřít spojení.

        Rozpracovaný JSON-RPC dispatch ještě doběhne a jeho odpověď se odešle;
        pak se WebSocket zavře a ``run_forever`` skončí (bez reconnectu).
        Druhé zavolání (opakovaný signál) ukončí proces okamžitě.
        """
        if self._shutdown_event.is_set():
            logger.warning("Forced shutdown (%s): exiting immediately", reason or "signal")
            os._exit(1)
        logger.info(
            "Graceful shutdown requested (%s): closing WebSocket, no new messages will be accepted",
            reason or "signal",
        )
        self._shutdown_event.set()

    async def run_forever(self) -> None:
        attempts = 0
        delay = self.settings.websocket_reconnect_initial_delay
        while self.settings.websocket_reconnect_max_attempts == 0 or attempts < self.settings.websocket_reconnect_max_attempts:
            if self._shutdown_event.is_set():
                return
            attempts += 1
            try:
                await self._run_once()
                if self._shutdown_event.is_set():
                    return
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
                if self._shutdown_event.is_set():
                    return
                await self._sleep_unless_shutdown(delay)
                delay = min(delay * 2, self.settings.websocket_reconnect_max_delay)

    async def _sleep_unless_shutdown(self, delay: float) -> None:
        sleep_task = asyncio.ensure_future(asyncio.sleep(delay))
        shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())
        try:
            await asyncio.wait({sleep_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleep_task.cancel()
            shutdown_task.cancel()
            await asyncio.gather(sleep_task, shutdown_task, return_exceptions=True)

    async def _run_once(self) -> None:
        import websockets
        from websockets.exceptions import ConnectionClosedOK

        assert self.settings.agentis_ws_endpoint is not None
        async with websockets.connect(
            self.settings.agentis_ws_endpoint,
            additional_headers=self._headers(),
            ping_interval=self.settings.websocket_heartbeat_interval,
            max_size=self.settings.websocket_max_message_size,
        ) as websocket:
            logger.info("Passive WebSocket connected adapter_id=%s", self.settings.agentis_adapter_id)
            shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())
            try:
                while True:
                    recv_task = asyncio.ensure_future(websocket.recv())
                    done, _ = await asyncio.wait({recv_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
                    if recv_task not in done:
                        # Shutdown během čekání na další zprávu — nic rozpracovaného není.
                        recv_task.cancel()
                        await asyncio.gather(recv_task, return_exceptions=True)
                        break
                    try:
                        raw_message = recv_task.result()
                    except ConnectionClosedOK:
                        return
                    # Už přijatou zprávu zpracujeme i během shutdownu, aby se neztratila.
                    response = await self.dispatch_message(raw_message)
                    if response is not None:
                        await websocket.send(json.dumps(response))
                    if self._shutdown_event.is_set():
                        break
            finally:
                shutdown_task.cancel()
                await asyncio.gather(shutdown_task, return_exceptions=True)
            logger.info(
                "Passive WebSocket closed after shutdown request adapter_id=%s",
                self.settings.agentis_adapter_id,
            )

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


def _install_signal_handlers(client: PassiveWebSocketClient) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, client.request_shutdown, sig.name)
        except NotImplementedError:
            # Platformy bez add_signal_handler (Windows) — fallback na sync handler.
            signal.signal(sig, lambda signum, _frame: client.request_shutdown(signal.Signals(signum).name))


async def run_passive_websocket(
    *, settings: Settings, dispatch: Mapping[str, JsonRpcRoute], service_container: Any
) -> None:
    """Běží WebSocket transport a po žádosti o shutdown nechá doběhnout běžící práci.

    SIGTERM/SIGINT zavře spojení (žádné nové zprávy), pak se čeká na běžící
    agenty a workflow až ``settings.shutdown_grace_period`` sekund (0 = bez limitu).
    """
    client = PassiveWebSocketClient(settings=settings, dispatch=dispatch, service_container=service_container)
    _install_signal_handlers(client)
    await client.run_forever()
    await asyncio.to_thread(drain_running_work, service_container, timeout=settings.shutdown_grace_period)
