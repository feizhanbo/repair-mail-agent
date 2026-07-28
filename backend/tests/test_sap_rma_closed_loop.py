from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import (
    CustomerServicePolicy,
    ExportSap,
    RepairTicket,
    TicketRelayExport,
    TicketRma,
    TicketRmaItem,
)
from app.services import customer_policies, external_relay, sap_rma


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _PollSession:
    def __init__(self, export: TicketRelayExport, ticket: RepairTicket, lines: list[ExportSap]):
        self.export = export
        self.ticket = ticket
        self.lines = lines
        self.added: list[object] = []

    async def get(self, model, object_id, **_kwargs):
        if model is TicketRelayExport and object_id == self.export.id:
            return self.export
        if model is RepairTicket and object_id == self.ticket.id:
            return self.ticket
        return None

    async def execute(self, _statement):
        return _ScalarRows(self.lines)

    async def scalar(self, _statement):
        return None

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for index, value in enumerate(self.added, start=1):
            if isinstance(value, TicketRma) and value.id is None:
                value.id = 100 + index


def _batch_fixture(rma_status: str = "waiting_sap"):
    now = datetime(2026, 7, 28, 2, 0, 0)
    export = TicketRelayExport(
        id=10,
        ticket_id=20,
        ticket_version=3,
        payload_hash="a" * 64,
        payload_snapshot={},
        status="accepted",
        exported_at=now,
        created_at=now,
    )
    ticket = RepairTicket(
        id=20,
        ticket_no="T-20",
        version=3,
        current_status_code="ready_for_export",
        rma_status=rma_status,
        safety_check_hash="a" * 64,
        sn_validation_hash="b" * 64,
    )
    lines = [
        ExportSap(
            id=31,
            ticket_id=20,
            ticket_item_id=41,
            relay_export_id=10,
            ticket_version=3,
            submission_key="submission-1",
            payload_hash="1" * 64,
            status="accepted",
            remote_call_id="call-1",
            sn="SN0001",
            submitted_at=now,
            policy_snapshot={"repair_price": "1200", "currency": "CNY"},
        ),
        ExportSap(
            id=32,
            ticket_id=20,
            ticket_item_id=42,
            relay_export_id=10,
            ticket_version=3,
            submission_key="submission-2",
            payload_hash="2" * 64,
            status="accepted",
            remote_call_id="call-2",
            sn="SN0002",
            submitted_at=now,
            policy_snapshot={"repair_price": "1200", "currency": "CNY"},
        ),
    ]
    return export, ticket, lines


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("2026072801", None),
        ("2026023001", "RMA_NUMBER_DATE_INVALID"),
        ("2026072800", "RMA_NUMBER_SEQUENCE_INVALID"),
        ("RMA20260728", "RMA_NUMBER_FORMAT_INVALID"),
        ("202607281", "RMA_NUMBER_FORMAT_INVALID"),
    ],
)
def test_rma_number_is_validated_but_never_allocated(value: str, error: str | None) -> None:
    if error is None:
        assert sap_rma.validate_rma_no(value) == value
    else:
        with pytest.raises(ValueError, match=error):
            sap_rma.validate_rma_no(value)


def test_two_sn_with_same_rma_create_one_ticket_rma(monkeypatch: pytest.MonkeyPatch) -> None:
    export, ticket, lines = _batch_fixture()
    session = _PollSession(export, ticket, lines)

    async def poll(_call_id: str):
        return {"status": "rma_received", "rma_no": "2026072801"}

    enqueue = AsyncMock(return_value=SimpleNamespace(id=88))
    monkeypatch.setattr(sap_rma, "poll_rma_from_relay", poll)
    monkeypatch.setattr(sap_rma, "enqueue_job", enqueue)
    monkeypatch.setattr(sap_rma, "notify_ticket_once", AsyncMock())

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "rma_received"
    assert result["rma_no"] == "2026072801"
    assert len([row for row in session.added if isinstance(row, TicketRma)]) == 1
    assert len([row for row in session.added if isinstance(row, TicketRmaItem)]) == 2
    assert {line.rma_no for line in lines} == {"2026072801"}
    enqueue.assert_awaited_once()


def test_two_sn_with_different_rma_require_manual_review(monkeypatch: pytest.MonkeyPatch) -> None:
    export, ticket, lines = _batch_fixture()
    session = _PollSession(export, ticket, lines)

    async def poll(call_id: str):
        return {
            "status": "rma_received",
            "rma_no": "2026072801" if call_id == "call-1" else "2026072802",
        }

    async def transition(_session, *, ticket, **_kwargs):
        ticket.current_status_code = "manual_review"
        return ticket

    enqueue = AsyncMock()
    monkeypatch.setattr(sap_rma, "poll_rma_from_relay", poll)
    monkeypatch.setattr(sap_rma, "transition_ticket", transition)
    monkeypatch.setattr(sap_rma, "enqueue_job", enqueue)

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "manual_review"
    assert result["error_code"] == "MULTIPLE_RMA_NUMBERS_REQUIRE_MANUAL_REVIEW"
    assert ticket.current_status_code == "manual_review"
    enqueue.assert_not_awaited()


def test_partial_rma_backfill_stays_recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    export, ticket, lines = _batch_fixture()
    session = _PollSession(export, ticket, lines)

    async def poll(call_id: str):
        if call_id == "call-1":
            return {"status": "rma_received", "rma_no": "2026072801"}
        return {"status": "waiting_rma", "rma_no": None}

    monkeypatch.setattr(sap_rma, "poll_rma_from_relay", poll)

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "waiting_rma"
    assert result["next_poll_seconds"] == 300
    assert lines[0].status == "rma_received"
    assert lines[1].status == "waiting_rma"


def test_invalid_rma_backfill_moves_ticket_to_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    export, ticket, lines = _batch_fixture()
    session = _PollSession(export, ticket, lines)

    async def poll(_call_id: str):
        return {"status": "rma_received", "rma_no": "2026130101"}

    async def transition(_session, *, ticket, **_kwargs):
        ticket.current_status_code = "manual_review"
        return ticket

    monkeypatch.setattr(sap_rma, "poll_rma_from_relay", poll)
    monkeypatch.setattr(sap_rma, "transition_ticket", transition)

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "manual_review"
    assert result["error_code"] == "RMA_NUMBER_DATE_INVALID"
    assert lines[0].status == "manual_review"
    assert ticket.current_status_code == "manual_review"


def test_rma_already_used_by_another_ticket_requires_manual_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export, ticket, lines = _batch_fixture()
    session = _PollSession(export, ticket, lines)
    existing = TicketRma(id=70, ticket_id=999, rma_no="2026072801", status="sent")

    async def scalar(_statement):
        return existing

    async def poll(_call_id: str):
        return {"status": "rma_received", "rma_no": "2026072801"}

    async def transition(_session, *, ticket, **_kwargs):
        ticket.current_status_code = "manual_review"
        return ticket

    session.scalar = scalar
    enqueue = AsyncMock()
    monkeypatch.setattr(sap_rma, "poll_rma_from_relay", poll)
    monkeypatch.setattr(sap_rma, "transition_ticket", transition)
    monkeypatch.setattr(sap_rma, "enqueue_job", enqueue)

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "manual_review"
    assert result["error_code"] == "RMA_NUMBER_ALREADY_LINKED_TO_OTHER_TICKET"
    assert export.status == "manual_review"
    assert all(line.status == "manual_review" for line in lines)
    enqueue.assert_not_awaited()


def test_unconfirmed_tianjin_address_blocks_export(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sap_rma.settings, "RMA_DEFAULT_TIANJIN_ADDRESS", "")
    monkeypatch.setattr(sap_rma.settings, "RMA_DEFAULT_TIANJIN_CONTACT", "")
    monkeypatch.setattr(sap_rma.settings, "RMA_DEFAULT_TIANJIN_PHONE", "")

    with pytest.raises(ValueError, match="TIANJIN_SHIPPING_ADDRESS_NOT_CONFIGURED"):
        sap_rma._address_details("tianjin")


class _PolicySession:
    def __init__(self, candidates, default_policy=None):
        self.candidates = candidates
        self.default_policy = default_policy

    async def execute(self, _statement):
        return _ScalarRows(self.candidates)

    async def scalar(self, _statement):
        return self.default_policy


def _policy(code: str, kind: str, price: str) -> CustomerServicePolicy:
    return CustomerServicePolicy(
        id=abs(hash(code)) % 10_000 + 1,
        policy_code=code,
        customer_code="CM-TEST",
        policy_type=kind,
        repair_price=Decimal(price),
        currency="CNY",
        tax_rate=Decimal("13"),
        shipping_fee_text="one-way charge/单次收费",
        enabled=True,
    )


def test_default_out_of_warranty_policy_is_1200_cny() -> None:
    default_policy = _policy("default-oow", "default", "1200")
    default_policy.customer_code = "*"
    result = asyncio.run(
        customer_policies.resolve_customer_policy(
            _PolicySession([], default_policy),
            customer_code="CM-NORMAL",
            requested_on=date(2026, 7, 28),
            in_warranty=False,
        )
    )

    assert result["status"] == "resolved"
    assert Decimal(result["policy"]["repair_price"]) == Decimal("1200.00")
    assert result["policy"]["currency"] == "CNY"
    assert result["policy"]["tax_rate"] == "13"


def test_free_and_special_price_overlap_requires_manual_review() -> None:
    result = asyncio.run(
        customer_policies.resolve_customer_policy(
            _PolicySession(
                [
                    _policy("free", "permanent_free", "0"),
                    _policy("special", "special_out_of_warranty", "1100"),
                ]
            ),
            customer_code="CM-TEST",
            requested_on=date(2026, 7, 28),
            in_warranty=False,
        )
    )

    assert result["status"] == "conflict"
    assert result["error_code"] == "CUSTOMER_POLICY_FREE_PRICE_CONFLICT"


def test_sqlserver_table_mode_requires_remote_submission_key_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_ENABLED", True)
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_HOST", "sql.test")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_DATABASE", "rma")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_USER", "user")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_PASSWORD", "secret")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_DRIVER", "ODBC Driver 18")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_SN_TABLE", "sn")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_SN_PRIMARY_KEY", "id")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN", "updatedAt")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_RESULT_MODE", "table")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_RESULT_TARGET", "exported")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_CALL_ID_COLUMN", "callID")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_RMA_COLUMN", "U_CustomerNum")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_RESULT_UNIQUE_COLUMN", "")
    monkeypatch.setattr(
        external_relay.settings,
        "RELAY_SQLSERVER_RESULT_COLUMN_MAP",
        {"sn": "internalSN"},
    )

    result = external_relay.relay_configuration_status()

    assert result["configured"] is False
    assert "RELAY_SQLSERVER_RESULT_UNIQUE_COLUMN" in result["missing"]
