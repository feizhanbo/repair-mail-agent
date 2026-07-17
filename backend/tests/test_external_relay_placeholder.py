from __future__ import annotations

import pytest

from app.config import settings
from app.services import external_relay
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

    assert result == {"status": "disabled", "configured": False, "synced_count": 0}


@pytest.mark.anyio
async def test_raw_parse_result_push_is_deprecated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RELAY_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "RELAY_BASE_URL", "")
    monkeypatch.setattr(settings, "RELAY_API_KEY", "")

    result = await push_ai_parse_result_to_relay(object(), parse_result_id=123)

    assert result == {
        "status": "deprecated",
        "parse_result_id": 123,
        "message": "Use validated ticket relay export",
    }


def test_sqlserver_status_never_exposes_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_ENABLED", True)
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_PASSWORD", "do-not-return-this")
    status = external_relay.relay_configuration_status()

    assert status["status"] in {"misconfigured", "configured"}
    assert "do-not-return-this" not in repr(status)
    assert "password" not in {key.lower() for key in status}


def test_sqlserver_query_uses_validated_identifiers_and_bound_values(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    class Cursor:
        description = [("sn",), ("id",), ("updated_at",)]

        def execute(self, sql, params):
            seen["sql"] = sql
            seen["params"] = params
            return self

        def fetchall(self):
            return []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(settings, "RELAY_SQLSERVER_SN_SCHEMA", "dbo")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_SN_TABLE", "sn_source")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_SN_PRIMARY_KEY", "id")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN", "updated_at")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_SN_COLUMN_MAP", {
        "sn": "sn",
        "customer_code": "customer_code",
        "customer_name": "customer_name",
        "material_code": "material_code",
        "asset_status": "asset_status",
    })
    monkeypatch.setattr(external_relay, "_connect", lambda: Connection())

    external_relay._fetch_sn_rows(
        cursor={"updated_at": "2026-07-16T00:00:00", "primary_key": 99},
        full=False,
    )

    assert "[dbo].[sn_source]" in seen["sql"]
    assert "2026-07-16T00:00:00" not in seen["sql"]
    assert seen["params"][1:] == ["2026-07-16T00:00:00", "2026-07-16T00:00:00", 99]
    with pytest.raises(external_relay.RelayConfigurationError):
        external_relay._identifier("sn_source; DROP TABLE users")
