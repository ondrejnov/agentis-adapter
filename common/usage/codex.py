from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CHATGPT_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


@dataclass
class CodexAuthData:
    auth_path: Path | None
    access_token: str
    refresh_token: str | None
    account_id: str | None
    label: str | None = None


class CodexUsageUnavailable(Exception):
    pass


def get_codex_usage() -> dict[str, Any]:
    auths = _load_codex_auth_accounts()
    accounts: list[dict[str, Any]] = []
    first_limits: dict[str, Any] | None = None
    for index, auth in enumerate(auths):
        account = _fetch_account_usage(auth, index)
        accounts.append(account)
        if account.get("available") and first_limits is None:
            first_limits = account["limits"]
    if first_limits is None:
        raise CodexUsageUnavailable("Codex usage API request failed for all configured accounts.")
    return {"available": True, "fetched_at": datetime.now(timezone.utc).isoformat(), "limits": first_limits, "accounts": accounts}


def _fetch_account_usage(auth: CodexAuthData, index: int) -> dict[str, Any]:
    try:
        payload = _fetch_usage(auth.access_token, auth.account_id)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 401 or not auth.refresh_token:
            return _usage_account_error(auth, index, "Codex usage API request failed.")
        try:
            auth = _refresh_auth(auth)
            payload = _fetch_usage(auth.access_token, auth.account_id)
        except (CodexUsageUnavailable, requests.HTTPError) as refresh_exc:
            return _usage_account_error(auth, index, str(refresh_exc) or "Codex usage API request failed after token refresh.")
    except CodexUsageUnavailable as exc:
        return _usage_account_error(auth, index, str(exc))
    return {"available": True, "id": auth.account_id or f"account-{index + 1}", "label": auth.label or auth.account_id or f"Account {index + 1}", "limits": _parse_limits(payload)}


def _usage_account_error(auth: CodexAuthData, index: int, error: str) -> dict[str, Any]:
    return {"available": False, "id": auth.account_id or f"account-{index + 1}", "label": auth.label or auth.account_id or f"Account {index + 1}", "error": error, "limits": {"primary": None, "secondary": None}}


def _load_codex_auth_accounts() -> list[CodexAuthData]:
    configured_accounts = _load_configured_codex_accounts()
    return configured_accounts if configured_accounts else _load_default_codex_auth_accounts()


def _load_default_codex_auth_accounts() -> list[CodexAuthData]:
    codex_dir = _codex_auth_dir()
    numbered_auth_paths = sorted(codex_dir.glob("auth[0-9]*.json"))
    if numbered_auth_paths:
        return [_load_codex_auth(path, fallback_label=path.stem) for path in numbered_auth_paths]
    return [_load_codex_auth(codex_dir / "auth.json")]


def _load_configured_codex_accounts() -> list[CodexAuthData]:
    raw = os.getenv("CODEX_USAGE_ACCOUNTS")
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexUsageUnavailable("CODEX_USAGE_ACCOUNTS must be a JSON array.") from exc
    if not isinstance(data, list):
        raise CodexUsageUnavailable("CODEX_USAGE_ACCOUNTS must be a JSON array.")
    accounts: list[CodexAuthData] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise CodexUsageUnavailable("CODEX_USAGE_ACCOUNTS items must be objects.")
        accounts.append(_load_configured_codex_account(item, index))
    return accounts


def _load_configured_codex_account(item: dict[str, Any], index: int) -> CodexAuthData:
    label = _optional_str(item.get("label"))
    auth_path = _optional_str(item.get("auth_path"))
    if auth_path:
        auth = _load_codex_auth(Path(auth_path).expanduser())
        auth.label = label or auth.label
        return auth
    access_token = _optional_str(item.get("access_token"))
    if not access_token:
        raise CodexUsageUnavailable(f"CODEX_USAGE_ACCOUNTS[{index}] requires auth_path or access_token.")
    return CodexAuthData(auth_path=None, access_token=access_token, refresh_token=_optional_str(item.get("refresh_token")), account_id=_optional_str(item.get("account_id")), label=label)


def _load_codex_auth(auth_path: Path, fallback_label: str | None = None) -> CodexAuthData:
    if not auth_path.exists():
        raise CodexUsageUnavailable("Codex auth.json was not found.")
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexUsageUnavailable("Codex auth.json could not be read.") from exc
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise CodexUsageUnavailable("ChatGPT Codex authentication is required.")
    refresh_token = tokens.get("refresh_token")
    account_id = tokens.get("account_id") or data.get("account_id")
    return CodexAuthData(auth_path=auth_path, access_token=access_token.strip(), refresh_token=refresh_token.strip() if isinstance(refresh_token, str) and refresh_token.strip() else None, account_id=account_id.strip() if isinstance(account_id, str) and account_id.strip() else None, label=_optional_str(data.get("label")) or _optional_str(data.get("email")) or fallback_label or auth_path.parent.name)


def _codex_auth_dir() -> Path:
    codex_home = os.getenv("CODEX_HOME")
    return Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"


def _fetch_usage(access_token: str, account_id: str | None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}", "User-Agent": "codex-cli"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        response = requests.get(CHATGPT_USAGE_URL, headers=headers, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise CodexUsageUnavailable("Codex usage API returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise CodexUsageUnavailable("Codex usage API returned an invalid payload.")
    return payload


def _refresh_auth(auth: CodexAuthData) -> CodexAuthData:
    if not auth.refresh_token or not auth.auth_path:
        raise CodexUsageUnavailable("Codex token refresh requires auth_path and refresh token.")
    try:
        response = requests.post(CHATGPT_TOKEN_URL, json={"client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": auth.refresh_token}, headers={"Content-Type": "application/json", "User-Agent": "codex-cli"}, timeout=10)
        response.raise_for_status()
        refresh_payload = response.json()
    except (requests.HTTPError, requests.JSONDecodeError) as exc:
        raise CodexUsageUnavailable("Codex token refresh failed.") from exc
    data = json.loads(auth.auth_path.read_text(encoding="utf-8"))
    tokens = data.setdefault("tokens", {})
    access_token = refresh_payload.get("access_token")
    refresh_token = refresh_payload.get("refresh_token")
    if isinstance(access_token, str) and access_token.strip():
        tokens["access_token"] = access_token.strip()
    if isinstance(refresh_token, str) and refresh_token.strip():
        tokens["refresh_token"] = refresh_token.strip()
    data["last_refresh"] = datetime.now(timezone.utc).isoformat()
    auth.auth_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    refreshed = _load_codex_auth(auth.auth_path)
    refreshed.label = auth.label
    return refreshed


def _parse_limits(payload: dict[str, Any]) -> dict[str, Any]:
    rate_limit = _unwrap(payload.get("rate_limit"))
    if not isinstance(rate_limit, dict):
        return {"primary": None, "secondary": None}
    return {"primary": _parse_window(_unwrap(rate_limit.get("primary_window"))), "secondary": _parse_window(_unwrap(rate_limit.get("secondary_window")))}


def _parse_window(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    return {"used_percent": _as_float(window.get("used_percent")), "window_minutes": _as_int(window.get("window_minutes")), "resets_at": _as_int(window.get("resets_at")) or _as_int(window.get("reset_at"))}


def _unwrap(value: Any) -> Any:
    while isinstance(value, list) and len(value) == 1:
        value = value[0]
    return value


def _optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None
