from __future__ import annotations

from uuid import UUID

import pytest

from app.config import settings
from app.integrations.sap_middleware import (
    ExternalRmaSubmissionItem,
    SapTransactionError,
    SapUnknownCommitStateError,
)
from app.integrations.sap_middleware.sqlserver import SqlServerSapMiddlewareAdapter, _identifier
from app.services.external_relay import (
    push_ai_parse_result_to_relay,
    relay_configuration_status,
    sync_sn_assets_from_relay,
)


@pytest.mark.anyio
async def test_relay_sn_sync_returns_disabled_when_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_ENABLED", False)
    result = await sync_sn_assets_from_relay(object())
    assert result == {"status": "disabled", "configured": False, "synced_count": 0}


@pytest.mark.anyio
async def test_raw_parse_result_push_is_deprecated() -> None:
    result = await push_ai_parse_result_to_relay(object(), parse_result_id=123)
    assert result["status"] == "deprecated"
    assert result["parse_result_id"] == 123
    assert "SourceRequestID" in result["message"]


def test_configuration_requires_source_request_column(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_ENABLED", True)
    monkeypatch.setattr(settings, "RELAY_ADAPTER", "sqlserver")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN", "")
    status = relay_configuration_status()
    assert status["configured"] is False
    assert "RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN" in status["missing"]


def test_identifier_rejects_sql_fragments() -> None:
    with pytest.raises(Exception, match="SAP_IDENTIFIER_INVALID"):
        _identifier("sn_source; DROP TABLE users")


class _Cursor:
    def __init__(self, fail_on: int | None = None):
        self.calls: list[tuple[str, object]] = []
        self.fail_on = fail_on

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if self.fail_on and len(self.calls) == self.fail_on:
            raise ValueError("known constraint failure")
        return self


class _Connection:
    def __init__(self, *, fail_on: int | None = None, rollback_fails: bool = False):
        self.cursor_value = _Cursor(fail_on)
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.rollback_fails = rollback_fails

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True
        if self.rollback_fails:
            raise OSError("connection lost")

    def close(self):
        self.closed = True


def _items() -> list[ExternalRmaSubmissionItem]:
    return [
        ExternalRmaSubmissionItem(
            source_request_id=UUID("11111111-1111-4111-8111-111111111111"),
            sn="SN-1",
            payload={"sn": "SN-1", "customer_code": "CM1"},
        ),
        ExternalRmaSubmissionItem(
            source_request_id=UUID("22222222-2222-4222-8222-222222222222"),
            sn="SN-2",
            payload={"sn": "SN-2", "customer_code": "CM1"},
        ),
    ]


def _configure_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_RESULT_SCHEMA", "dbo")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_RESULT_TARGET", "exported")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN", "SourceRequestID")
    monkeypatch.setattr(
        settings,
        "RELAY_SQLSERVER_RESULT_COLUMN_MAP",
        {"sn": "internalSN", "customer_code": "customer"},
    )


def test_sqlserver_submits_all_sn_in_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_mapping(monkeypatch)
    connection = _Connection()
    adapter = SqlServerSapMiddlewareAdapter()
    monkeypatch.setattr(adapter, "_connect", lambda: connection)
    adapter._submit_sync(_items())
    assert connection.committed is True
    assert connection.rolled_back is False
    assert len(connection.cursor_value.calls) == 2
    assert "[SourceRequestID]" in connection.cursor_value.calls[0][0]
    assert "CallID" not in connection.cursor_value.calls[0][0]


def test_sqlserver_rolls_back_whole_batch_on_known_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_mapping(monkeypatch)
    connection = _Connection(fail_on=2)
    adapter = SqlServerSapMiddlewareAdapter()
    monkeypatch.setattr(adapter, "_connect", lambda: connection)
    with pytest.raises(SapTransactionError):
        adapter._submit_sync(_items())
    assert connection.committed is False
    assert connection.rolled_back is True


def test_sqlserver_reports_unknown_when_rollback_cannot_be_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_mapping(monkeypatch)
    connection = _Connection(fail_on=2, rollback_fails=True)
    adapter = SqlServerSapMiddlewareAdapter()
    monkeypatch.setattr(adapter, "_connect", lambda: connection)
    with pytest.raises(SapUnknownCommitStateError):
        adapter._submit_sync(_items())
