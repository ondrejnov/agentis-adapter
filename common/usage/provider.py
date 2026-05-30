from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import requests

from common.config import Settings
from common.agentis import AgentisJsonRpcClient, AgentisJsonRpcError
from common.usage.claude import ClaudeUsageUnavailable, get_claude_usage
from common.usage.codex import CodexUsageUnavailable, get_codex_usage

PROVIDERS = {
    "codex": {"title": "Codex limits", "vendor": "OpenAI", "loader": get_codex_usage, "error": CodexUsageUnavailable},
    "claude": {
        "title": "Claude Code limits",
        "vendor": "Anthropic",
        "loader": get_claude_usage,
        "error": ClaudeUsageUnavailable,
        "skip_unavailable": True,
    },
}


class ProviderUsageSyncError(RuntimeError):
    pass


class ProviderUsageSyncService:
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()

    def sync_provider_usage(self, params: Any) -> dict[str, Any]:
        return self.sync(params.providers)

    def sync(self, providers: list[str] | None = None) -> dict[str, Any]:
        provider_codes = providers or list(PROVIDERS.keys())
        synced: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for code in provider_codes:
            config = PROVIDERS.get(code)
            if config is None:
                failed.append({"code": code, "error": "Unknown provider."})
                continue

            usage = self._load_usage(config)
            if config.get("skip_unavailable") and not usage.get("available"):
                failed.append({"code": code, "error": usage.get("error") or "Usage data is unavailable."})
                continue
            try:
                result = self._save_usage(code, config, usage)
            except ProviderUsageSyncError as exc:
                failed.append({"code": code, "error": str(exc)})
                continue
            synced.append({"code": code, "available": bool(usage.get("available")), "result": result})

        return {"synced": synced, "failed": failed}

    def _load_usage(self, config: dict[str, Any]) -> dict[str, Any]:
        try:
            return config["loader"]()
        except config["error"] as exc:
            return {
                "available": False,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
                "limits": {"primary": None, "secondary": None},
            }

    def _save_usage(self, code: str, config: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
        endpoint = self.settings.agentis_endpoint
        if not endpoint:
            raise ProviderUsageSyncError("AGENTIS_ENDPOINT is not configured.")
        try:
            client = AgentisJsonRpcClient(endpoint=endpoint, token=self.settings.agentis_token, session=self.session)
            result = client.call(
                method="provider.save_usage",
                params={
                    "data": {
                        "code": code,
                        "title": config["title"],
                        "vendor": config["vendor"],
                        "usage": usage,
                    }
                },
                request_id=f"provider-sync-{code}-{uuid4().hex}",
            )
        except AgentisJsonRpcError as exc:
            raise ProviderUsageSyncError(str(exc)) from exc
        return result if isinstance(result, dict) else {"ok": True, "result": result}
