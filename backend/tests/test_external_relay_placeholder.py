from __future__ import annotations

import pytest

from app.config import settings
from app.services.external_relay import push_ai_parse_result_to_relay, sync_sn_assets_from_relay


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_relay_sn_sync_returns_disabled_when_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RELAY_SN_SYNC_ENABLED", False)
    monkeypatch.setattr(settings, "RELAY_BASE_URL", "https://relay.example.com")
    monkeypatch.setattr(settings, "RELAY_API_KEY", "secret-relay-key")

    result = await sync_sn_assets_from_relay(object())

    assert result == {"status": "disabled", "configured": True, "synced_count": 0}


@pytest.mark.anyio
async def test_relay_push_returns_not_configured_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RELAY_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "RELAY_BASE_URL", "")
    monkeypatch.setattr(settings, "RELAY_API_KEY", "")

    result = await push_ai_parse_result_to_relay(object(), parse_result_id=123)

    assert result == {"status": "not_configured", "configured": False, "parse_result_id": 123}
