from __future__ import annotations

from typing import Any
from uuid import uuid4

import requests

from common.config import get_settings

AUTH_HEADER = "X-Auth-Token"


class AgentisJsonRpcError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class AgentisJsonRpcClient:
    def __init__(
        self,
        endpoint: str,
        token: str | None = None,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        self.endpoint = self._normalize_endpoint(endpoint)
        self.timeout = timeout
        self.session = session or requests.Session()
        self._owns_session = session is None
        if token:
            self.session.headers.update({AUTH_HEADER: str(token)})

    def __enter__(self) -> AgentisJsonRpcClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        value = endpoint.strip().rstrip("/")
        if not value:
            raise ValueError("endpoint must not be empty")
        if value.endswith("/api"):
            return value
        return f"{value}/api"

    def call(
        self,
        method: str,
        params: Any = None,
        *,
        request_id: Any | None = None,
        timeout: float | None = None,
    ) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": request_id if request_id is not None else f"agentis-{uuid4().hex}",
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        try:
            response = self.session.post(
                self.endpoint, json=payload, timeout=timeout if timeout is not None else self.timeout
            )
        except requests.RequestException as exc:
            raise AgentisJsonRpcError(f"Agentis JSON-RPC request failed: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = response.text

        if response.status_code >= 400:
            raise AgentisJsonRpcError(
                f"Agentis returned HTTP {response.status_code}",
                status_code=response.status_code,
                details=body,
            )

        if not isinstance(body, dict):
            raise AgentisJsonRpcError(
                "Agentis returned an invalid JSON-RPC response",
                status_code=response.status_code,
                details=body,
            )

        error = body.get("error")
        if error is not None:
            message = "Agentis returned a JSON-RPC error"
            if isinstance(error, dict) and error.get("message"):
                message = str(error["message"])
            raise AgentisJsonRpcError(message, status_code=response.status_code, details=error)

        return body.get("result")


class AgentisRunLogger:
    def __init__(
        self,
        run_id: str,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        timeout: float = 10.0,
        client: AgentisJsonRpcClient | None = None,
    ) -> None:
        normalized_run_id = run_id.strip()
        if not normalized_run_id:
            raise ValueError("run_id must not be empty")

        self.run_id = normalized_run_id
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

        if client is None:
            settings = get_settings()
            resolved_endpoint = endpoint or settings.agentis_endpoint
            if not resolved_endpoint:
                raise ValueError("endpoint must not be empty")
            self._client = AgentisJsonRpcClient(
                endpoint=resolved_endpoint,
                token=settings.agentis_token if token is None else token,
                timeout=timeout,
            )

    def __enter__(self) -> AgentisRunLogger:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def event(
        self,
        kind: str,
        status: str,
        *,
        message: str | None = None,
        data: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> Any:
        normalized_kind = kind.strip()
        normalized_status = status.strip()
        if not normalized_kind:
            raise ValueError("kind must not be empty")
        if not normalized_status:
            raise ValueError("status must not be empty")
        if self._client is None:
            raise RuntimeError("Agentis run logger is closed")

        normalized_event_id = event_id or f"{normalized_kind}:{uuid4().hex}"
        print(
            {
                "run_id": self.run_id,
                "kind": normalized_kind,
                "status": normalized_status,
                "event_id": normalized_event_id,
                "message": message,
                "data": data or {},
            }
        )
        return self._client.call(
            method="run.adapter_event",
            params={
                "run_id": self.run_id,
                "kind": normalized_kind,
                "status": normalized_status,
                "event_id": normalized_event_id,
                "message": message,
                "data": data or {},
            },
            request_id=f"agentis-run-log-{self.run_id}-{normalized_event_id}-{normalized_status}",
            timeout=self.timeout,
        )

    def started(
        self,
        kind: str = "system",
        *,
        message: str | None = None,
        data: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> Any:
        return self.event(kind=kind, status="started", message=message, data=data, event_id=event_id)

    def success(
        self,
        kind: str = "system",
        *,
        message: str | None = None,
        data: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> Any:
        return self.event(kind=kind, status="success", message=message, data=data, event_id=event_id)

    def failed(
        self,
        kind: str = "system",
        *,
        message: str | None = None,
        data: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> Any:
        return self.event(kind=kind, status="failed", message=message, data=data, event_id=event_id)
