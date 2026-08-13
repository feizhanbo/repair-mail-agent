from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.models import Email, RepairTicket, RepairTicketItem, SnAsset
from app.services import ticket_safety


class ScalarSession:
    def __init__(self, *values):
        self.values = list(values)

    async def scalar(self, _statement):
        return self.values.pop(0) if self.values else None


class Rows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ticket() -> RepairTicket:
    return RepairTicket(
        id=1,
        ticket_no="RMATEST0001",
        current_status_code="parsed",
        customer_code="C001",
        customer_name="Test Customer",
        contact_person="Test Contact",
        contact_phone="000-0000",
        contact_email="customer@example.com",
        request_date=date(2026, 7, 1),
        mailing_address="TEST DATA - NO SHIPMENT",
        problem_description="Synthetic failure only",
        version=3,
        sn_validation_status="pending",
    )


def _item(sn: str = "TESTSN00000001", *, item_id: int = 11, line_no: int = 1) -> RepairTicketItem:
    return RepairTicketItem(
        id=item_id,
        ticket_id=1,
        line_no=line_no,
        sn=sn,
        material_code="TEST-PART",
        material_name="Synthetic Part",
        quantity=1,
        failure_description="Synthetic failure only",
        validation_status="pending",
    )


@pytest.mark.anyio
async def test_sn_validation_uses_local_mirror_and_never_calls_sql_when_disabled(monkeypatch) -> None:
    ticket = _ticket()
    item = _item()
    asset = SnAsset(
        id=21,
        sn=item.sn,
        customer_code=ticket.customer_code,
        customer_name=ticket.customer_name,
        material_code=item.material_code,
        material_name=item.material_name,
        asset_status="valid",
        warranty_start_date=date(2026, 1, 1),
        warranty_end_date=date(2026, 12, 31),
        source_system="local",
    )

    async def fake_ticket_and_items(_session, _ticket_id):
        return ticket, [item]

    monkeypatch.setattr(settings, "RELAY_SQLSERVER_ENABLED", False)
    monkeypatch.setattr(ticket_safety, "_ticket_and_items", fake_ticket_and_items)

    report = await ticket_safety.build_sn_validation_report(ScalarSession(asset, None), ticket_id=1)

    assert report["passed"] is True
    assert report["snapshot"]["source"] == "local_sn_assets"
    assert report["snapshot"]["checks"][0]["warranty_end_date"] == "2026-12-31"
    assert ticket.current_status_code == "parsed"


@pytest.mark.anyio
async def test_persisted_sn_hash_uses_asset_enriched_material_state(monkeypatch) -> None:
    ticket = _ticket()
    ticket.customer_code = None
    item = _item()
    item.material_code = None
    item.material_name = None
    asset = SnAsset(
        id=21,
        sn=item.sn,
        customer_code="C001",
        customer_name=ticket.customer_name,
        material_code="TEST-PART",
        material_name="Synthetic Part",
        asset_status="valid",
        source_system="local",
    )

    async def fake_ticket_and_items(_session, _ticket_id):
        return ticket, [item]

    class Session(ScalarSession):
        async def execute(self, _statement):
            return Rows([asset])

        def add(self, _value):
            return None

    monkeypatch.setattr(settings, "RELAY_SQLSERVER_ENABLED", False)
    monkeypatch.setattr(ticket_safety, "_ticket_and_items", fake_ticket_and_items)

    report = await ticket_safety.build_sn_validation_report(Session(asset, None), ticket_id=1, persist=True)

    assert report["passed"] is True
    assert item.material_code == "TEST-PART"
    assert report["input_hash"] == ticket_safety._stable_hash(ticket_safety._sn_input_snapshot(ticket, [item]))


@pytest.mark.anyio
async def test_customer_source_uses_exact_sender_exclusions_not_whole_internal_domain(monkeypatch) -> None:
    ticket = _ticket()
    ticket.thread_id = 9
    system_email = Email(id=31, thread_id=9, mail_direction="inbound", from_address="rmatest1@accotest.com")
    customer_email = Email(id=32, thread_id=9, mail_direction="inbound", from_address="bert.fei@accotest.com")

    class Session:
        async def execute(self, _statement):
            return Rows([system_email, customer_email])

        async def get(self, _model, _identity):
            return None

    monkeypatch.setattr(settings, "IMAP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "DEVICE_RECEIPT_TRUSTED_SENDERS", [])

    result = await ticket_safety._customer_source_email(Session(), ticket)

    assert result is customer_email


@pytest.mark.anyio
async def test_duplicate_sn_fails_core_validation_before_asset_lookup(monkeypatch) -> None:
    ticket = _ticket()
    items = [_item(item_id=11, line_no=1), _item(item_id=12, line_no=2)]

    async def fake_ticket_and_items(_session, _ticket_id):
        return ticket, items

    monkeypatch.setattr(settings, "RELAY_SQLSERVER_ENABLED", False)
    monkeypatch.setattr(ticket_safety, "_ticket_and_items", fake_ticket_and_items)
    report = await ticket_safety.build_sn_validation_report(ScalarSession(), ticket_id=1)

    assert report["passed"] is False
    assert report["errors"] == {
        "items.1.sn": "duplicate_sn",
        "items.2.sn": "duplicate_sn",
    }


@pytest.mark.anyio
async def test_sn_failure_stops_export_validation_immediately(monkeypatch) -> None:
    ticket = _ticket()

    class Session:
        async def get(self, model, identity, **_kwargs):
            assert model is RepairTicket and identity == 1
            return ticket

    async def fake_sn(*_args, **_kwargs):
        return {"ticket_id": 1, "status": "failed", "report": {"errors": {"items.1.sn": "sn_not_found"}}}

    async def forbidden_safety(*_args, **_kwargs):
        raise AssertionError("remaining safety checks must not run after SN failure")

    monkeypatch.setattr(ticket_safety, "validate_ticket_sn_core", fake_sn)
    monkeypatch.setattr(
        ticket_safety,
        "resolve_and_snapshot_ticket_policy",
        AsyncMock(return_value={"status": "resolved"}),
    )
    monkeypatch.setattr(
        ticket_safety,
        "resolve_ticket_return_routes",
        AsyncMock(return_value={"status": "resolved", "items": []}),
    )
    monkeypatch.setattr(ticket_safety, "build_safety_report", forbidden_safety)

    result = await ticket_safety.validate_and_mark_ready_for_export(Session(), ticket_id=1, user_id=7)

    assert result["status"] == "sn_validation_failed"
    assert result["jobs"] == []


@pytest.mark.anyio
async def test_manual_ready_resolution_clears_historical_parse_markers(monkeypatch) -> None:
    ticket = _ticket()
    ticket.current_status_code = "manual_review"
    ticket.missing_fields = {"request_date": "required"}
    ticket.conflict_fields = {"postal_code": ["100094", "610000"]}

    class Session:
        async def get(self, model, identity, **_kwargs):
            assert model is RepairTicket and identity == 1
            return ticket

    async def fake_sn(*_args, **_kwargs):
        return {"ticket_id": 1, "status": "passed", "report": {"passed": True}}

    async def fake_safety(*_args, **_kwargs):
        return {
            "passed": True,
            "errors": {},
            "sn_source": "local_sn_assets",
            "snapshot_hash": "a" * 64,
            "snapshot": {
                "ticket_version": ticket.version + 1,
                "language_code": "zh-CN",
                "rma_required": False,
            },
        }

    async def fake_transition(_session, *, ticket, **_kwargs):
        ticket.current_status_code = "ready_for_export"
        ticket.version += 1

    monkeypatch.setattr(ticket_safety, "validate_ticket_sn_core", fake_sn)
    monkeypatch.setattr(ticket_safety, "build_safety_report", fake_safety)
    monkeypatch.setattr(ticket_safety, "transition_ticket", fake_transition)
    monkeypatch.setattr(
        ticket_safety,
        "resolve_and_snapshot_ticket_policy",
        AsyncMock(return_value={"status": "resolved"}),
    )
    monkeypatch.setattr(
        ticket_safety,
        "resolve_ticket_return_routes",
        AsyncMock(return_value={"status": "resolved", "items": []}),
    )

    result = await ticket_safety.validate_and_mark_ready_for_export(
        Session(),
        ticket_id=1,
        user_id=7,
        resolving_task_id=19,
    )

    assert result["status"] == "ready_for_export"
    assert result["sap_required"] is False
    assert result["export_id"] is None
    assert ticket.missing_fields == {}
    assert ticket.conflict_fields == {}


@pytest.mark.anyio
async def test_changed_sn_input_marks_previous_validation_stale(monkeypatch) -> None:
    ticket = _ticket()
    item = _item()
    old_hash = ticket_safety._stable_hash(ticket_safety._sn_input_snapshot(ticket, [item]))
    ticket.sn_validation_status = "passed"
    ticket.sn_validation_hash = old_hash
    ticket.sn_validation_snapshot = {"source": "local_sn_assets", "checks": []}
    item.material_code = "CHANGED-PART"
    source = Email(
        id=31,
        mailbox_account="test",
        message_id="<root@example.com>",
        mail_direction="inbound",
        from_address=ticket.contact_email,
        subject="Synthetic repair",
        intent_type="new_repair",
    )

    async def fake_ticket_and_items(_session, _ticket_id):
        return ticket, [item]

    async def fake_source(_session, _ticket):
        return source

    monkeypatch.setattr(ticket_safety, "_ticket_and_items", fake_ticket_and_items)
    monkeypatch.setattr(ticket_safety, "_customer_source_email", fake_source)
    report = await ticket_safety.build_safety_report(object(), ticket_id=1)

    assert report["passed"] is False
    assert report["errors"]["sn_validation"] == "stale"
    assert ticket.sn_validation_status == "stale"
