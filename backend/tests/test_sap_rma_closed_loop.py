from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

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
from app.integrations.sap_middleware import ExternalRmaResult


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _PollSession:
    def __init__(
        self,
        export: TicketRelayExport,
        ticket: RepairTicket,
        lines: list[ExportSap],
        scalar_results: list[object | None] | None = None,
        rma_rows: list[TicketRma] | None = None,
    ):
        self.export = export
        self.ticket = ticket
        self.lines = lines
        self.added: list[object] = []
        self.scalar_results = list(scalar_results or [])
        self.rma_rows = list(rma_rows or [])

    async def get(self, model, object_id, **_kwargs):
        if model is TicketRelayExport and object_id == self.export.id:
            return self.export
        if model is RepairTicket and object_id == self.ticket.id:
            return self.ticket
        return None

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity") if statement.column_descriptions else None
        if entity is TicketRma:
            return _ScalarRows(self.rma_rows)
        return _ScalarRows(self.lines)

    async def scalar(self, _statement):
        return self.scalar_results.pop(0) if self.scalar_results else None

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for index, value in enumerate(self.added, start=1):
            if isinstance(value, TicketRma) and value.id is None:
                value.id = 100 + index


def _batch_fixture(rma_status: str = "waiting_sap"):
    now = sap_rma.utcnow()
    export = TicketRelayExport(
        id=10,
        ticket_id=20,
        ticket_version=3,
        payload_hash="a" * 64,
        payload_snapshot={},
        status="waiting_sap_result",
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
        customer_code="CM-TEST",
        request_date=date(2026, 7, 28),
    )
    lines = [
        ExportSap(
            id=31,
            ticket_id=20,
            ticket_item_id=41,
            relay_export_id=10,
            ticket_version=3,
            source_request_id="11111111-1111-4111-8111-111111111111",
            payload_hash="1" * 64,
            status="waiting_sap_result",
            sn="SN0001",
            submitted_at=now,
            policy_snapshot={
                "repair_price": "1200",
                "currency": "CNY",
                "return_route_status": "resolved",
                "shipping_address": "Beijing",
                "shipping_contact": "Alice",
                "shipping_phone": "010-1",
            },
        ),
        ExportSap(
            id=32,
            ticket_id=20,
            ticket_item_id=42,
            relay_export_id=10,
            ticket_version=3,
            source_request_id="22222222-2222-4222-8222-222222222222",
            payload_hash="2" * 64,
            status="waiting_sap_result",
            sn="SN0002",
            submitted_at=now,
            policy_snapshot={
                "repair_price": "1200",
                "currency": "CNY",
                "return_route_status": "resolved",
                "shipping_address": "Beijing",
                "shipping_contact": "Alice",
                "shipping_phone": "010-1",
            },
        ),
    ]
    return export, ticket, lines


class _ResultAdapter:
    def __init__(self, mapping: dict[str, str | None]):
        self.mapping = mapping

    async def find_records_by_source_request_ids(self, source_request_ids):
        return [
            ExternalRmaResult(
                source_request_id=value,
                sn=None,
                rma_no=self.mapping.get(str(value)),
            )
            for value in source_request_ids
            if str(value) in self.mapping
        ]


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

    enqueue = AsyncMock(return_value=SimpleNamespace(id=88))
    monkeypatch.setattr(
        sap_rma,
        "create_sap_middleware_adapter",
        lambda: _ResultAdapter({line.source_request_id: "2026072801" for line in lines}),
    )
    monkeypatch.setattr(sap_rma, "enqueue_job", enqueue)
    monkeypatch.setattr(sap_rma, "notify_ticket_once", AsyncMock())

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "rma_received"
    assert result["rma_no"] == "2026072801"
    assert len([row for row in session.added if isinstance(row, TicketRma)]) == 1
    assert len([row for row in session.added if isinstance(row, TicketRmaItem)]) == 2
    assert {line.rma_no for line in lines} == {"2026072801"}
    enqueue.assert_awaited_once()


def test_unsent_existing_rma_uses_latest_export_policy_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export, ticket, lines = _batch_fixture()
    for line in lines:
        line.policy_snapshot = {
            **(line.policy_snapshot or {}),
            "policy_type": "default",
        }
    existing = TicketRma(
        id=70,
        ticket_id=ticket.id,
        rma_no="2026072801",
        status="received",
        policy_snapshot={"lines": [{"policy_type": "special_out_of_warranty"}]},
    )
    session = _PollSession(export, ticket, lines, rma_rows=[existing])
    monkeypatch.setattr(
        sap_rma,
        "create_sap_middleware_adapter",
        lambda: _ResultAdapter({line.source_request_id: "2026072801" for line in lines}),
    )
    monkeypatch.setattr(
        sap_rma,
        "enqueue_job",
        AsyncMock(return_value=SimpleNamespace(id=88)),
    )
    monkeypatch.setattr(sap_rma, "notify_ticket_once", AsyncMock())

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "rma_received"
    assert existing.policy_snapshot == {
        "lines": [line.policy_snapshot for line in lines]
    }


def test_two_sn_with_different_rma_require_manual_review(monkeypatch: pytest.MonkeyPatch) -> None:
    export, ticket, lines = _batch_fixture()
    session = _PollSession(export, ticket, lines)

    async def transition(_session, *, ticket, **_kwargs):
        ticket.current_status_code = "manual_review"
        return ticket

    enqueue = AsyncMock()
    monkeypatch.setattr(
        sap_rma,
        "create_sap_middleware_adapter",
        lambda: _ResultAdapter(
            {lines[0].source_request_id: "2026072801", lines[1].source_request_id: "2026072802"}
        ),
    )
    monkeypatch.setattr(sap_rma, "transition_ticket", transition)
    monkeypatch.setattr(sap_rma, "enqueue_job", enqueue)
    monkeypatch.setattr(
        sap_rma,
        "start_external_operation",
        AsyncMock(return_value=SimpleNamespace(status="running", remote_reference=None)),
    )

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "manual_review"
    assert result["error_code"] == "MULTIPLE_RMA_NUMBERS_REQUIRE_MANUAL_REVIEW"
    assert ticket.current_status_code == "manual_review"
    enqueue.assert_not_awaited()


def test_partial_rma_backfill_stays_recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    export, ticket, lines = _batch_fixture()
    session = _PollSession(export, ticket, lines)

    monkeypatch.setattr(
        sap_rma,
        "create_sap_middleware_adapter",
        lambda: _ResultAdapter(
            {lines[0].source_request_id: "2026072801", lines[1].source_request_id: None}
        ),
    )

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "waiting_rma"
    assert result["next_poll_seconds"] == 300
    assert lines[0].status == "rma_received"
    assert lines[1].status == "waiting_rma"


def test_invalid_rma_backfill_moves_ticket_to_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    export, ticket, lines = _batch_fixture()
    session = _PollSession(export, ticket, lines)

    async def transition(_session, *, ticket, **_kwargs):
        ticket.current_status_code = "manual_review"
        return ticket

    monkeypatch.setattr(
        sap_rma,
        "create_sap_middleware_adapter",
        lambda: _ResultAdapter({line.source_request_id: "2026130101" for line in lines}),
    )
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
    existing = TicketRma(
        id=70,
        ticket_id=999,
        rma_no="2026072801",
        customer_code="CM-OTHER",
        repair_business_date=date(2026, 7, 28),
        status="sent",
    )

    async def transition(_session, *, ticket, **_kwargs):
        ticket.current_status_code = "manual_review"
        return ticket

    session.rma_rows = [existing]
    enqueue = AsyncMock()
    monkeypatch.setattr(
        sap_rma,
        "create_sap_middleware_adapter",
        lambda: _ResultAdapter({line.source_request_id: "2026072801" for line in lines}),
    )
    monkeypatch.setattr(sap_rma, "transition_ticket", transition)
    monkeypatch.setattr(sap_rma, "enqueue_job", enqueue)
    monkeypatch.setattr(
        sap_rma,
        "start_external_operation",
        AsyncMock(return_value=SimpleNamespace(status="running", remote_reference=None)),
    )

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "manual_review"
    assert result["error_code"] == "RMA_CROSS_TICKET_BUSINESS_IDENTITY_CONFLICT"
    assert export.status == "manual_review"
    assert all(line.status == "manual_review" for line in lines)
    enqueue.assert_not_awaited()


def test_same_customer_and_business_date_can_reuse_rma_across_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export, ticket, lines = _batch_fixture()
    existing = TicketRma(
        id=70,
        ticket_id=999,
        rma_no="2026072801",
        customer_code=ticket.customer_code,
        repair_business_date=ticket.request_date,
        status="sent",
    )
    session = _PollSession(export, ticket, lines, rma_rows=[existing])
    monkeypatch.setattr(
        sap_rma,
        "create_sap_middleware_adapter",
        lambda: _ResultAdapter({line.source_request_id: "2026072801" for line in lines}),
    )
    monkeypatch.setattr(
        sap_rma,
        "enqueue_job",
        AsyncMock(return_value=SimpleNamespace(id=88)),
    )
    monkeypatch.setattr(sap_rma, "notify_ticket_once", AsyncMock())

    result = asyncio.run(sap_rma.poll_export_batch(session, export_id=export.id))

    assert result["status"] == "rma_received"
    created = [row for row in session.added if isinstance(row, TicketRma)]
    assert len(created) == 1
    assert created[0].ticket_id == ticket.id
    assert created[0].rma_no == existing.rma_no


def test_unknown_submit_all_source_ids_found_resumes_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export, ticket, lines = _batch_fixture()
    export.status = "submit_unknown"
    export.next_retry_at = sap_rma.utcnow() + timedelta(minutes=5)
    for line in lines:
        line.status = "submit_unknown"
    session = _PollSession(export, ticket, lines)
    monkeypatch.setattr(
        sap_rma,
        "create_sap_middleware_adapter",
        lambda: _ResultAdapter({line.source_request_id: None for line in lines}),
    )
    enqueue = AsyncMock(return_value=SimpleNamespace(id=88))
    monkeypatch.setattr(sap_rma, "enqueue_job", enqueue)

    result = asyncio.run(
        sap_rma.reconcile_uncertain_submission(
            session,
            export_id=export.id,
            reason="test",
            user_id=None,
        )
    )

    assert result["status"] == "waiting_sap_result"
    assert export.status == "waiting_sap_result"
    assert all(line.status == "waiting_sap_result" for line in lines)
    enqueue.assert_awaited_once()


def test_unknown_submit_no_source_ids_after_second_check_retries_same_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export, ticket, lines = _batch_fixture()
    export.status = "submit_unknown"
    export.next_retry_at = sap_rma.utcnow() - timedelta(seconds=1)
    original_ids = [line.source_request_id for line in lines]
    for line in lines:
        line.status = "submit_unknown"
    session = _PollSession(export, ticket, lines)
    monkeypatch.setattr(
        sap_rma,
        "create_sap_middleware_adapter",
        lambda: _ResultAdapter({}),
    )
    enqueue = AsyncMock(return_value=SimpleNamespace(id=89))
    monkeypatch.setattr(sap_rma, "enqueue_job", enqueue)

    result = asyncio.run(
        sap_rma.reconcile_uncertain_submission(
            session,
            export_id=export.id,
            reason="second_check",
            user_id=7,
        )
    )

    assert result["status"] == "pending"
    assert [line.source_request_id for line in lines] == original_ids
    assert all(line.status == "pending" for line in lines)
    enqueue.assert_awaited_once()


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
        customer_name="Test Customer",
        policy_type=kind,
        charge_status=customer_policies.charge_status_for_policy_type(kind),
        customer_scope="domestic",
        repair_price=Decimal(price),
        currency="CNY",
        tax_rate=Decimal("13"),
        shipping_fee_text="one-way charge/单次收费",
        enabled=True,
    )


def test_missing_customer_specific_policy_requires_manual_resolution() -> None:
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

    assert result["status"] == "missing"
    assert result["error_code"] == "CUSTOMER_POLICY_MISSING"


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
    assert result["error_code"] == "CUSTOMER_POLICY_CONFLICT"


def test_sqlserver_table_mode_requires_source_request_id_but_not_call_id(
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
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN", "SourceRequestID")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_CALL_ID_COLUMN", "")
    monkeypatch.setattr(external_relay.settings, "RELAY_SQLSERVER_RMA_COLUMN", "U_CustomerNum")
    monkeypatch.setattr(
        external_relay.settings,
        "RELAY_SQLSERVER_RESULT_COLUMN_MAP",
        {"sn": "internalSN"},
    )

    result = external_relay.relay_configuration_status()

    assert result["configured"] is True
    assert result["missing"] == []
