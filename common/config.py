from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


def _get_env(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = env.get(name)
    return value if value is not None else default


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""

    try:
        parts = shlex.split(value, comments=True, posix=True)
    except ValueError:
        return value

    if not parts:
        return ""
    return " ".join(parts)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            continue

        key = key.strip()
        if not key:
            continue

        values[key] = _parse_env_value(value)

    return values


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    default_namespace: str
    app_host: str | None
    manifest_path: Path
    worktree_root: Path
    public_base_url: str | None
    agentis_endpoint: str | None
    agentis_token: str | None
    namespace_prefix: str = "Task"
    claude_run_mode: str = "local"
    claude_pod_selector: str = "deployment/opencode"
    claude_pod_container: str = "opencode"
    kubectl_command: str = "kubectl"
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


def _public_base_url(env: Mapping[str, str], default_port: int) -> str | None:
    explicit = _get_env(env, "ADAPTER_PUBLIC_URL")
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")

    service_name = _get_env(env, "K8S_SERVICE_NAME")
    namespace = _get_env(env, "K8S_NAMESPACE")
    if not service_name or not namespace:
        return None

    service_port = (_get_env(env, "K8S_SERVICE_PORT", str(default_port)) or str(default_port)).strip()
    return f"http://{service_name.strip()}.{namespace.strip()}.svc.cluster.local:{service_port}"


def _build_settings() -> Settings:
    project_root = Path(__file__).parent.parent.resolve()
    env_file = project_root / ".env"
    env: dict[str, str] = {**_read_env_file(env_file), **os.environ}
    port = int(_get_env(env, "ADAPTER_PORT", "8001") or "8001")
    manifest_path = Path(
        _get_env(env, "ADAPTER_MANIFEST_PATH", str(project_root / "kubernetes")) or str(project_root / "kubernetes")
    ).resolve()
    return Settings(
        host=_get_env(env, "ADAPTER_HOST", "0.0.0.0") or "0.0.0.0",
        port=port,
        default_namespace=_get_env(env, "ADAPTER_NAMESPACE", "agentis") or "agentis",
        app_host=_get_env(env, "ADAPTER_APP_HOST"),
        manifest_path=manifest_path,
        worktree_root=Path(
            _get_env(env, "ADAPTER_WORKTREE_ROOT", str(project_root / "worktrees")) or str(project_root / "worktrees")
        ).resolve(),
        public_base_url=_public_base_url(env, port),
        agentis_endpoint=_get_env(env, "AGENTIS_ENDPOINT", "http://127.0.0.1:8891"),
        agentis_token=_get_env(env, "AGENTIS_TOKEN", "1234"),
        namespace_prefix=_get_env(env, "ADAPTER_NAMESPACE_PREFIX", "Task") or "Task",
        claude_run_mode=(_get_env(env, "CLAUDE_RUN_MODE", "kubernetes") or "kubernetes").strip().lower(),
        claude_pod_selector=_get_env(env, "CLAUDE_POD_SELECTOR", "deployment/opencode") or "deployment/opencode",
        claude_pod_container=_get_env(env, "CLAUDE_POD_CONTAINER", "opencode") or "opencode",
        kubectl_command=_get_env(env, "KUBECTL_COMMAND", "kubectl") or "kubectl",
        agentis_ws_endpoint=_get_env(env, "AGENTIS_WS_ENDPOINT"),
        agentis_adapter_id=_get_env(env, "AGENTIS_ADAPTER_ID"),
        websocket_heartbeat_interval=float(_get_env(env, "AGENTIS_WS_HEARTBEAT_INTERVAL", "30") or "30"),
        websocket_max_message_size=int(
            _get_env(env, "AGENTIS_WS_MAX_MESSAGE_SIZE", str(64 * 1024 * 1024)) or str(64 * 1024 * 1024)
        ),
        websocket_reconnect_initial_delay=float(_get_env(env, "AGENTIS_WS_RECONNECT_INITIAL_DELAY", "1") or "1"),
        websocket_reconnect_max_delay=float(_get_env(env, "AGENTIS_WS_RECONNECT_MAX_DELAY", "30") or "30"),
        websocket_reconnect_max_attempts=int(_get_env(env, "AGENTIS_WS_RECONNECT_MAX_ATTEMPTS", "0") or "0"),
        agentiscode_command=_get_env(env, "AGENTISCODE_COMMAND", "agentiscode") or "agentiscode",
        agentiscode_adapter=(_get_env(env, "AGENTISCODE_ADAPTER", "opencode") or "opencode").strip().lower(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return _build_settings()
