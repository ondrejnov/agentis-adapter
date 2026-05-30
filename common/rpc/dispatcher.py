from __future__ import annotations

import asyncio
import sys
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from common.rpc.jsonrpc import AgentJsonRpcException, AgentJsonRpcService, validate_params


PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_REQUEST = -32600
INTERNAL_ERROR = -32603


@dataclass(frozen=True)
class JsonRpcRoute:
    params_model: type[BaseModel]
    handler_name: str
    service_attr: str = "agent_jsonrpc_service"


@dataclass(frozen=True)
class JsonRpcDispatchResult:
    body: dict[str, Any]
    http_status: int


def result_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def log_internal_error(message: str, exc: BaseException, request_id: Any, method: str | None, params: Any) -> None:
    sanitized_params = AgentJsonRpcService._sanitize_for_log(params)
    context = [f"request_id={request_id!r}"]
    if method:
        context.append(f"method={method}")
    if sanitized_params not in (None, {}):
        context.append(f"params={sanitized_params!r}")

    sys.stderr.write(f"{message} ({', '.join(context)})\n")
    traceback.print_exception(exc, file=sys.stderr)
    sys.stderr.flush()


def http_status_for_agent_error(code: int) -> int:
    if code == 404:
        return 404
    if code >= 500 or code == INTERNAL_ERROR:
        return 500
    return 400


async def dispatch_jsonrpc_payload(
    payload: Any,
    dispatch: Mapping[str, JsonRpcRoute],
    service_container: Any,
    *,
    catch_not_implemented: bool = False,
) -> JsonRpcDispatchResult:
    request_id = None
    method = None
    params = None

    if not isinstance(payload, dict):
        return JsonRpcDispatchResult(error_response(None, INVALID_REQUEST, "Request must be an object"), 400)

    request_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0":
        return JsonRpcDispatchResult(error_response(request_id, INVALID_REQUEST, "Server supports only JSON-RPC 2.0"), 400)

    method = payload.get("method")
    params = payload.get("params")
    if not method:
        return JsonRpcDispatchResult(error_response(request_id, INVALID_REQUEST, "Missing method"), 400)

    entry = dispatch.get(method)
    if entry is None:
        return JsonRpcDispatchResult(error_response(request_id, METHOD_NOT_FOUND, f"Method not found '{method}'"), 404)

    try:
        validated_params = validate_params(entry.params_model, params)
        service = getattr(service_container, entry.service_attr)
        result = await asyncio.to_thread(getattr(service, entry.handler_name), validated_params)
    except AgentJsonRpcException as exc:
        http_status = http_status_for_agent_error(exc.code)
        if http_status == 500:
            log_internal_error("JSON-RPC method failed", exc, request_id, method, params)
        return JsonRpcDispatchResult(error_response(request_id, exc.code, exc.message, exc.data), http_status)
    except NotImplementedError as exc:
        if catch_not_implemented:
            return JsonRpcDispatchResult(error_response(request_id, INTERNAL_ERROR, f"Not implemented: {exc}"), 501)
        log_internal_error("Unhandled JSON-RPC method error", exc, request_id, method, params)
        return JsonRpcDispatchResult(error_response(request_id, INTERNAL_ERROR, "Internal error", str(exc)), 500)
    except Exception as exc:
        log_internal_error("Unhandled JSON-RPC method error", exc, request_id, method, params)
        return JsonRpcDispatchResult(error_response(request_id, INTERNAL_ERROR, "Internal error", str(exc)), 500)

    return JsonRpcDispatchResult(result_response(request_id, result), 200)
