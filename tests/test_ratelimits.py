from __future__ import annotations

import ratelimits


def test_parse_args_accepts_provider_codes_without_script_changes():
    args = ratelimits.parse_args(["custom-provider"])

    assert args.providers == ["custom-provider"]


def test_main_passes_requested_provider_codes(monkeypatch, capsys):
    synced_providers: list[str] | None = None

    class FakeProviderUsageSyncService:
        def __init__(self, settings):
            assert settings == "settings"

        def sync(self, providers):
            nonlocal synced_providers
            synced_providers = providers
            return {"synced": [{"code": "custom-provider"}], "failed": []}

    monkeypatch.setattr(ratelimits, "get_settings", lambda: "settings")
    monkeypatch.setattr(ratelimits, "ProviderUsageSyncService", FakeProviderUsageSyncService)
    monkeypatch.setattr(ratelimits.sys, "argv", ["ratelimits.py", "custom-provider"])

    exit_code = ratelimits.main()

    assert exit_code == 0
    assert synced_providers == ["custom-provider"]
    assert "custom-provider" in capsys.readouterr().out
