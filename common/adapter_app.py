from __future__ import annotations

from collections.abc import Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common.config import Settings
from common.rpc.dispatcher import JsonRpcRoute, dispatch_jsonrpc_payload, error_response, log_internal_error
from common.rpc.session_registry import SessionContextRegistry


PARSE_ERROR = -32700


async def _handle_jsonrpc_request(
    request: Request,
    dispatch: Mapping[str, JsonRpcRoute],
    *,
    log_received_method: bool = False,
    catch_not_implemented: bool = False,
) -> JSONResponse:
    request_id = None
    method = None
    params = None
    try:
        payload = await request.json()
        if log_received_method:
            print(f"Received request: {payload['method']}")
    except Exception as exc:
        log_internal_error("Failed to parse JSON-RPC request", exc, request_id, method, params)
        return JSONResponse(
            error_response(None, PARSE_ERROR, f"Parse error: {exc}"),
            status_code=500,
        )

    result = await dispatch_jsonrpc_payload(
        payload,
        dispatch,
        request.app.state,
        catch_not_implemented=catch_not_implemented,
    )
    return JSONResponse(result.body, status_code=result.http_status)


def create_adapter_app(
    *,
    title: str,
    settings: Settings,
    configure_services: Callable[[FastAPI, Settings, SessionContextRegistry], None],
    dispatch: Mapping[str, JsonRpcRoute],
    internal_dispatch: Mapping[str, JsonRpcRoute] | None = None,
    version: str = "0.1.0",
) -> FastAPI:
    app = FastAPI(title=title, version=version)
    session_registry = SessionContextRegistry()
    app.state.session_registry = session_registry
    configure_services(app, settings, session_registry)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api")
    async def api(request: Request) -> JSONResponse:
        return await _handle_jsonrpc_request(request, dispatch, log_received_method=True)

    if internal_dispatch is not None:

        @app.post("/api-internal")
        async def api_internal(request: Request) -> JSONResponse:
            """JSON-RPC endpoint for requests coming from the adapter runtime."""
            return await _handle_jsonrpc_request(request, internal_dispatch, catch_not_implemented=True)

    return app
