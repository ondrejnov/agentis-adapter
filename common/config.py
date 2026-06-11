from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


def _get_env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value is not None else default


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    worktree_root: Path
    public_base_url: str | None
    agentis_endpoint: str | None
    agentis_token: str | None
    namespace_prefix: str = "Task"
    project_run_root: Path = Path("/tmp/agentis")
    kubectl_command: str = "kubectl"
    workflow_executor: str = "kubernetes"
    agentis_ws_endpoint: str | None = None
    agentis_adapter_id: str | None = None
    websocket_heartbeat_interval: float = 30.0
    websocket_max_message_size: int = 64 * 1024 * 1024
    websocket_reconnect_initial_delay: float = 1.0
    websocket_reconnect_max_delay: float = 30.0
    websocket_reconnect_max_attempts: int = 0
    agentiscode_command: str = "agentiscode"
    agentiscode_adapter: str = "claude"

    def validate_passive_websocket(self) -> None:
        if not self.agentis_ws_endpoint:
            raise ValueError("AGENTIS_WS_ENDPOINT is required for WebSocket transport")
        if not self.agentis_adapter_id:
            raise ValueError("AGENTIS_ADAPTER_ID is required for WebSocket transport")
        if not self.agentis_token:
            raise ValueError("AGENTIS_TOKEN is required for WebSocket transport")

        parsed = urlparse(self.agentis_ws_endpoint)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError("AGENTIS_WS_ENDPOINT must use ws:// or wss://")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme == "ws" and parsed.hostname not in local_hosts:
            raise ValueError("AGENTIS_WS_ENDPOINT must use wss:// for non-localhost endpoints")


def _public_base_url(default_port: int) -> str | None:
    explicit = _get_env("ADAPTER_PUBLIC_URL")
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")

    service_name = _get_env("K8S_SERVICE_NAME")
    namespace = _get_env("K8S_NAMESPACE")
    if not service_name or not namespace:
        return None

    service_port = (_get_env("K8S_SERVICE_PORT", str(default_port)) or str(default_port)).strip()
    return f"http://{service_name.strip()}.{namespace.strip()}.svc.cluster.local:{service_port}"


def _build_settings() -> Settings:
    project_root = Path(__file__).parent.parent.resolve()
    load_dotenv(project_root / ".env")
    print(_get_env("AGENTIS_ENDPOINT", "http://127.0.0.1:8891"))
    port = int(_get_env("ADAPTER_PORT", "8001") or "8001")
    return Settings(
        host=_get_env("ADAPTER_HOST", "0.0.0.0") or "0.0.0.0",
        port=port,
        worktree_root=Path(
            _get_env("ADAPTER_WORKTREE_ROOT", str(project_root / "worktrees")) or str(project_root / "worktrees")
        ).resolve(),
        public_base_url=_public_base_url(port),
        agentis_endpoint=_get_env("AGENTIS_ENDPOINT", "http://127.0.0.1:8891"),
        agentis_token=_get_env("AGENTIS_TOKEN", "1234"),
        namespace_prefix=_get_env("ADAPTER_NAMESPACE_PREFIX", "Task") or "Task",
        project_run_root=Path(_get_env("ADAPTER_PROJECT_RUN_ROOT", "/tmp/agentis") or "/tmp/agentis").resolve(),
        kubectl_command=_get_env("KUBECTL_COMMAND", "kubectl") or "kubectl",
        workflow_executor=(_get_env("WORKFLOW_EXECUTOR", "kubernetes") or "kubernetes").strip().lower(),
        agentis_ws_endpoint=_get_env("AGENTIS_WS_ENDPOINT"),
        agentis_adapter_id=_get_env("AGENTIS_ADAPTER_ID"),
        websocket_heartbeat_interval=float(_get_env("AGENTIS_WS_HEARTBEAT_INTERVAL", "30") or "30"),
        websocket_max_message_size=int(
            _get_env("AGENTIS_WS_MAX_MESSAGE_SIZE", str(64 * 1024 * 1024)) or str(64 * 1024 * 1024)
        ),
        websocket_reconnect_initial_delay=float(_get_env("AGENTIS_WS_RECONNECT_INITIAL_DELAY", "1") or "1"),
        websocket_reconnect_max_delay=float(_get_env("AGENTIS_WS_RECONNECT_MAX_DELAY", "30") or "30"),
        websocket_reconnect_max_attempts=int(_get_env("AGENTIS_WS_RECONNECT_MAX_ATTEMPTS", "0") or "0"),
        agentiscode_command=_get_env("AGENTISCODE_COMMAND", "agentiscode") or "agentiscode",
        agentiscode_adapter=(_get_env("AGENTISCODE_ADAPTER", "opencode") or "opencode").strip().lower(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return _build_settings()
