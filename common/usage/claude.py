from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FIVE_HOUR_WINDOW_MINUTES = 5 * 60
WEEKLY_WINDOW_MINUTES = 7 * 24 * 60
OFFICIAL_USAGE_TIMEOUT_SECONDS = 15
CLAUDE_OAUTH_REFRESH_MARGIN_SECONDS = 300
CLAUDE_OAUTH_BETA_HEADER = "oauth-2025-04-20"
CLAUDE_OAUTH_API_BASE_URL = "https://api.anthropic.com"
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_LOCAL_OAUTH_CLIENT_ID = "22422756-60c9-4084-8eb7-27705fd5cf9a"
CLAUDE_OAUTH_SCOPES = [
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]


@dataclass
class ClaudeUsageAccountConfig:
    usage_path: Path
    label: str | None = None
    account_id: str | None = None
    five_hour_token_limit: int | None = None
    weekly_token_limit: int | None = None


@dataclass(frozen=True)
class ClaudeUsageEvent:
    created_at: datetime
    tokens: int


class ClaudeUsageUnavailable(Exception):
    pass


def get_claude_usage() -> dict[str, Any]:
    official_account = _fetch_oauth_usage_account()
    if official_account is None:
        raise ClaudeUsageUnavailable("Claude Code online usage data is unavailable.")
    return {
        "available": True,
        "fetched_at": _now().isoformat(),
        "limits": official_account["limits"],
        "accounts": [official_account],
    }


def _build_account_usage(config: ClaudeUsageAccountConfig, index: int) -> dict[str, Any]:
    account_id = config.account_id or (f"account-{index + 1}" if index else "default")
    label = config.label or config.account_id or "Claude Code"
    try:
        events = _load_usage_events(config.usage_path)
    except ClaudeUsageUnavailable as exc:
        return _usage_account_error(account_id, label, str(exc))
    if not events:
        return _usage_account_error(account_id, label, "Claude Code usage data was not found.")
    return {"available": True, "id": account_id, "label": label, "limits": _calculate_limits(events, config)}


def _usage_account_error(account_id: str, label: str, error: str) -> dict[str, Any]:
    return {
        "available": False,
        "id": account_id,
        "label": label,
        "error": error,
        "limits": {"primary": None, "secondary": None},
    }


def _default_claude_usage_accounts() -> list[ClaudeUsageAccountConfig]:
    return [
        ClaudeUsageAccountConfig(
            usage_path=_claude_home() / "projects",
            account_id="default",
            label="Claude Code",
            five_hour_token_limit=_env_int("CLAUDE_CODE_5H_TOKEN_LIMIT"),
            weekly_token_limit=_env_int("CLAUDE_CODE_WEEKLY_TOKEN_LIMIT"),
        )
    ]


def _load_configured_claude_accounts() -> list[ClaudeUsageAccountConfig]:
    raw = os.getenv("CLAUDE_USAGE_ACCOUNTS")
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeUsageUnavailable("CLAUDE_USAGE_ACCOUNTS must be a JSON array.") from exc
    if not isinstance(data, list):
        raise ClaudeUsageUnavailable("CLAUDE_USAGE_ACCOUNTS must be a JSON array.")
    accounts: list[ClaudeUsageAccountConfig] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ClaudeUsageUnavailable("CLAUDE_USAGE_ACCOUNTS items must be objects.")
        accounts.append(_load_configured_claude_account(item, index))
    return accounts


def _load_configured_claude_account(item: dict[str, Any], index: int) -> ClaudeUsageAccountConfig:
    usage_path = _optional_str(item.get("usage_path")) or _optional_str(item.get("path"))
    if not usage_path:
        raise ClaudeUsageUnavailable(f"CLAUDE_USAGE_ACCOUNTS[{index}] requires usage_path.")
    return ClaudeUsageAccountConfig(
        usage_path=Path(usage_path).expanduser(),
        label=_optional_str(item.get("label")),
        account_id=_optional_str(item.get("account_id")) or _optional_str(item.get("id")),
        five_hour_token_limit=_as_positive_int(item.get("five_hour_token_limit"))
        or _env_int("CLAUDE_CODE_5H_TOKEN_LIMIT"),
        weekly_token_limit=_as_positive_int(item.get("weekly_token_limit"))
        or _env_int("CLAUDE_CODE_WEEKLY_TOKEN_LIMIT"),
    )


def _fetch_oauth_usage_account() -> dict[str, Any] | None:
    oauth_tokens = _load_claude_oauth_tokens()
    if not oauth_tokens:
        return None
    scopes = oauth_tokens.get("scopes")
    if not isinstance(scopes, list) or "user:inference" not in scopes:
        return None
    auth_headers = _get_claude_oauth_headers(oauth_tokens)
    if not auth_headers:
        return None
    payload = _http_json_request(
        f"{_claude_oauth_api_base_url()}/api/oauth/usage", {"Content-Type": "application/json", **auth_headers}
    )
    return _parse_claude_oauth_usage_payload(payload) if isinstance(payload, dict) else None


def _parse_claude_oauth_usage_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    primary = _normalize_claude_oauth_window(payload.get("five_hour"), FIVE_HOUR_WINDOW_MINUTES)
    secondary = _normalize_claude_oauth_window(
        payload.get("seven_day"), WEEKLY_WINDOW_MINUTES
    ) or _normalize_claude_oauth_window(payload.get("seven_day_sonnet"), WEEKLY_WINDOW_MINUTES)
    if primary is None and secondary is None:
        return None
    account = {
        "available": True,
        "id": "default",
        "label": "Claude Code",
        "limits": {"primary": primary, "secondary": secondary},
    }
    extra_usage = _normalize_claude_extra_usage(payload.get("extra_usage"))
    if extra_usage is not None:
        account["extra_usage"] = extra_usage
    return account


def _normalize_claude_oauth_window(window: Any, window_minutes: int) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    utilization = _as_float(window.get("utilization"))
    resets_at = _parse_datetime(window.get("resets_at"))
    if utilization is None and resets_at is None:
        return None
    return {
        "used_percent": round(max(0.0, min(100.0, utilization)), 2) if utilization is not None else None,
        "used_tokens": None,
        "token_limit": None,
        "window_minutes": window_minutes,
        "resets_at": int(resets_at.timestamp()) if resets_at is not None else None,
    }


def _normalize_claude_extra_usage(extra_usage: Any) -> dict[str, Any] | None:
    if not isinstance(extra_usage, dict):
        return None
    return {
        "is_enabled": bool(extra_usage.get("is_enabled")),
        "monthly_limit": _as_float(extra_usage.get("monthly_limit")),
        "used_credits": _as_float(extra_usage.get("used_credits")),
        "utilization": _as_float(extra_usage.get("utilization")),
        "currency": _optional_str(extra_usage.get("currency")),
    }


def _load_claude_oauth_tokens() -> dict[str, Any] | None:
    try:
        payload = json.loads(_claude_credentials_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload.get("claudeAiOauth") if isinstance(payload.get("claudeAiOauth"), dict) else None


def _get_claude_oauth_headers(oauth_tokens: dict[str, Any]) -> dict[str, str] | None:
    access_token = _optional_str(oauth_tokens.get("accessToken"))
    if access_token and not _oauth_token_expires_soon(oauth_tokens.get("expiresAt")):
        return {"Authorization": f"Bearer {access_token}", "anthropic-beta": CLAUDE_OAUTH_BETA_HEADER}
    refreshed_tokens = _refresh_claude_oauth_tokens(oauth_tokens)
    refreshed_access_token = _optional_str((refreshed_tokens or {}).get("accessToken"))
    if refreshed_access_token:
        return {"Authorization": f"Bearer {refreshed_access_token}", "anthropic-beta": CLAUDE_OAUTH_BETA_HEADER}
    if access_token:
        return {"Authorization": f"Bearer {access_token}", "anthropic-beta": CLAUDE_OAUTH_BETA_HEADER}
    return None


def _refresh_claude_oauth_tokens(oauth_tokens: dict[str, Any]) -> dict[str, Any] | None:
    refresh_token = _optional_str(oauth_tokens.get("refreshToken"))
    if not refresh_token:
        return None
    payload = _http_json_request(
        _claude_oauth_token_url(),
        {"Content-Type": "application/json"},
        method="POST",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _claude_oauth_client_id(),
            "scope": " ".join(CLAUDE_OAUTH_SCOPES),
        },
    )
    if not isinstance(payload, dict):
        return None
    access_token = _optional_str(payload.get("access_token"))
    expires_in = _as_int(payload.get("expires_in"))
    if not access_token or expires_in is None or expires_in <= 0:
        return None
    scope = payload.get("scope")
    refreshed_tokens = {
        "accessToken": access_token,
        "refreshToken": _optional_str(payload.get("refresh_token")) or refresh_token,
        "expiresAt": int((_now() + timedelta(seconds=expires_in)).timestamp() * 1000),
        "scopes": scope.split(" ")
        if isinstance(scope, str) and scope.strip()
        else oauth_tokens.get("scopes") or CLAUDE_OAUTH_SCOPES,
        "subscriptionType": oauth_tokens.get("subscriptionType"),
        "rateLimitTier": oauth_tokens.get("rateLimitTier"),
    }
    _save_claude_oauth_tokens(refreshed_tokens)
    return refreshed_tokens


def _load_usage_events(path: Path) -> list[ClaudeUsageEvent]:
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(path.rglob("*.jsonl"))
    else:
        raise ClaudeUsageUnavailable("Claude Code usage path does not exist.")
    events: list[ClaudeUsageEvent] = []
    for file_path in files:
        events.extend(_iter_usage_file_events(file_path))
    return events


def _iter_usage_file_events(file_path: Path) -> Iterable[ClaudeUsageEvent]:
    try:
        with file_path.open(encoding="utf-8") as handle:
            for line in handle:
                event = _parse_usage_line(line)
                if event:
                    yield event
    except OSError as exc:
        raise ClaudeUsageUnavailable("Claude Code usage data could not be read.") from exc


def _parse_usage_line(line: str) -> ClaudeUsageEvent | None:
    try:
        payload = json.loads(line.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    created_at = _parse_datetime(payload.get("timestamp") or payload.get("created_at") or payload.get("createdAt"))
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    message = payload.get("message")
    if usage is None and isinstance(message, dict) and isinstance(message.get("usage"), dict):
        usage = message["usage"]
    tokens = _usage_token_count(usage)
    if not created_at or tokens <= 0:
        return None
    return ClaudeUsageEvent(created_at=created_at, tokens=tokens)


def _usage_token_count(usage: dict[str, Any] | None) -> int:
    if not usage:
        return 0
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    )
    return sum(_as_non_negative_int(usage.get(key)) for key in keys)


def _calculate_limits(events: list[ClaudeUsageEvent], config: ClaudeUsageAccountConfig) -> dict[str, Any]:
    now = _now()
    primary_events = [event for event in events if now - timedelta(hours=5) <= event.created_at <= now]
    weekly_events = [event for event in events if now - timedelta(days=7) <= event.created_at <= now]
    primary_reset = min((event.created_at for event in primary_events), default=now) + timedelta(hours=5)
    weekly_reset = min((event.created_at for event in weekly_events), default=now) + timedelta(days=7)
    return {
        "primary": _build_window(
            sum(event.tokens for event in primary_events),
            config.five_hour_token_limit,
            FIVE_HOUR_WINDOW_MINUTES,
            primary_reset,
        ),
        "secondary": _build_window(
            sum(event.tokens for event in weekly_events), config.weekly_token_limit, WEEKLY_WINDOW_MINUTES, weekly_reset
        ),
    }


def _build_window(
    used_tokens: int, token_limit: int | None, window_minutes: int, resets_at: datetime
) -> dict[str, Any]:
    return {
        "used_percent": round((used_tokens / token_limit) * 100, 2) if token_limit and token_limit > 0 else None,
        "used_tokens": used_tokens,
        "token_limit": token_limit,
        "window_minutes": window_minutes,
        "resets_at": int(resets_at.timestamp()),
    }


def _http_json_request(
    url: str, headers: dict[str, str], method: str = "GET", data: dict[str, Any] | None = None
) -> Any:
    request_data = json.dumps(data).encode("utf-8") if data is not None else None
    request = Request(url, data=request_data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=OFFICIAL_USAGE_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except (HTTPError, URLError, OSError, TimeoutError) as e:
        print(e)
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _claude_home() -> Path:
    claude_home = os.getenv("CLAUDE_HOME")
    return Path(claude_home).expanduser() if claude_home else Path.home() / ".claude"


def _claude_credentials_path() -> Path:
    claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")
    return (
        Path(claude_config_dir).expanduser() / ".credentials.json"
        if claude_config_dir
        else _claude_home() / ".credentials.json"
    )


def _claude_oauth_api_base_url() -> str:
    return (
        _optional_str(os.getenv("CLAUDE_LOCAL_OAUTH_API_BASE"))
        or _optional_str(os.getenv("CLAUDE_CODE_CUSTOM_OAUTH_URL"))
        or CLAUDE_OAUTH_API_BASE_URL
    ).rstrip("/")


def _claude_oauth_token_url() -> str:
    local_base = _optional_str(os.getenv("CLAUDE_LOCAL_OAUTH_API_BASE"))
    if local_base:
        return f"{local_base.rstrip('/')}/v1/oauth/token"
    custom_base = _optional_str(os.getenv("CLAUDE_CODE_CUSTOM_OAUTH_URL"))
    return f"{custom_base.rstrip('/')}/v1/oauth/token" if custom_base else CLAUDE_OAUTH_TOKEN_URL


def _claude_oauth_client_id() -> str:
    override = _optional_str(os.getenv("CLAUDE_CODE_OAUTH_CLIENT_ID"))
    if override:
        return override
    return (
        CLAUDE_LOCAL_OAUTH_CLIENT_ID
        if _optional_str(os.getenv("CLAUDE_LOCAL_OAUTH_API_BASE"))
        else CLAUDE_OAUTH_CLIENT_ID
    )


def _save_claude_oauth_tokens(oauth_tokens: dict[str, Any]) -> None:
    credentials_path = _claude_credentials_path()
    try:
        payload = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    payload["claudeAiOauth"] = oauth_tokens
    temp_path = credentials_path.with_suffix(f"{credentials_path.suffix}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(credentials_path)


def _oauth_token_expires_soon(expires_at: Any) -> bool:
    expires_at_value = _as_int(expires_at)
    return (
        False
        if expires_at_value is None
        else expires_at_value
        <= int((_now() + timedelta(seconds=CLAUDE_OAUTH_REFRESH_MARGIN_SECONDS)).timestamp() * 1000)
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _env_int(name: str) -> int | None:
    return _as_positive_int(os.getenv(name))


def _as_positive_int(value: Any) -> int | None:
    parsed = _as_int(value)
    return parsed if parsed and parsed > 0 else None


def _as_non_negative_int(value: Any) -> int:
    parsed = _as_int(value)
    return max(0, parsed or 0)


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


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
