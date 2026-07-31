from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import (
    BoardCard,
    CustomerServicePolicy,
    ExportSap,
    RepairTicket,
    RepairTicketItem,
    SnAsset,
    TicketRelayExport,
)
from app.core.repair_items import normalize_board_code, normalize_board_name
from app.services import business_resolution, customer_policies, sap_rma


class ScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class QueueSession:
    def __init__(self, execute_rows=None, get_values=None):
        self.execute_rows = list(execute_rows or [])
        self.get_values = dict(get_values or {})
        self.added = []

    async def execute(self, _statement):
        return ScalarRows(self.execute_rows.pop(0))

    async def get(self, model, identity, **_kwargs):
        return self.get_values.get((model, identity))

    async def scalar(self, _statement):
        return None

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for index, value in enumerate(self.added, start=1):
            if getattr(value, "id", None) is None:
                value.id = 100 + index


def test_board_fields_use_nfkc_and_preserve_business_characters() -> None:
    assert normalize_board_code(" ｍ８１１７_plus ") == "M8117_PLUS"
    assert normalize_board_name(" DIO　 PLUS ") == "DIO PLUS"


def board(
    code: str,
    location: str,
    *,
    scope: str = "domestic",
    route_type: str = "board_rule",
    name: str = "Board",
) -> BoardCard:
    return BoardCard(
        id=abs(hash((code, name, location))) % 10_000 + 1,
        board_code=code,
        board_name=name,
        return_location=location,
        route_type=route_type,
        customer_scope=scope,
        material_code=code,
        material_name=name,
        need_ship_to_beijing=location == "beijing",
        shipping_address=f"{location} address",
        shipping_contact="Receiver",
        shipping_phone="010-1",
        status="active",
    )


@pytest.mark.anyio
async def test_overseas_route_does_not_require_board_fields() -> None:
    ticket = RepairTicket(id=1, ticket_no="T1", customer_scope="overseas")
    item = RepairTicketItem(id=2, ticket_id=1, line_no=1)
    session = QueueSession(execute_rows=[[
        board("*", "beijing", scope="overseas", route_type="scope_default")
    ]])

    result = await business_resolution.resolve_item_return_route(
        session,
        ticket=ticket,
        item=item,
    )

    assert result["status"] == "resolved"
    assert result["route_source"] == "overseas_default"
    assert item.return_location == "beijing"


@pytest.mark.anyio
async def test_domestic_route_uses_board_code_not_sap_material_code() -> None:
    ticket = RepairTicket(id=1, ticket_no="T1", customer_scope="domestic")
    item = RepairTicketItem(
        id=2,
        ticket_id=1,
        line_no=1,
        material_code="M8002",
        board_code=None,
    )

    result = await business_resolution.resolve_item_return_route(
        QueueSession(),
        ticket=ticket,
        item=item,
    )

    assert result["status"] == "needs_manual"
    assert result["message"] == "BOARD_INFORMATION_REQUIRED"


@pytest.mark.anyio
async def test_domestic_code_with_one_location_resolves_and_snapshots() -> None:
    ticket = RepairTicket(id=1, ticket_no="T1", customer_scope="domestic")
    item = RepairTicketItem(
        id=2,
        ticket_id=1,
        line_no=1,
        board_code="M8117",
        board_name="DIO_PLUS",
    )
    rows = [
        board("M8117", "tianjin", name="DIO"),
        board("M8117", "tianjin", name="DIO_PLUS"),
    ]

    result = await business_resolution.resolve_item_return_route(
        QueueSession(execute_rows=[rows]),
        ticket=ticket,
        item=item,
    )
    original_address = item.return_route_snapshot["return_address"]
    rows[1].shipping_address = "changed master address"

    assert result["status"] == "resolved"
    assert item.return_location == "tianjin"
    assert item.return_route_snapshot["return_address"] == original_address


@pytest.mark.anyio
async def test_domestic_board_can_resolve_beijing() -> None:
    ticket = RepairTicket(id=1, ticket_no="T1", customer_scope="domestic")
    item = RepairTicketItem(
        id=2,
        ticket_id=1,
        line_no=1,
        board_code="M8002",
        board_name="PVI",
    )

    result = await business_resolution.resolve_item_return_route(
        QueueSession(execute_rows=[[board("M8002", "beijing", name="PVI")]]),
        ticket=ticket,
        item=item,
    )

    assert result["status"] == "resolved"
    assert result["return_location"] == "beijing"


@pytest.mark.anyio
async def test_domestic_code_with_two_locations_requires_manual() -> None:
    ticket = RepairTicket(id=1, ticket_no="T1", customer_scope="domestic")
    item = RepairTicketItem(id=2, ticket_id=1, line_no=1, board_code="M1")

    result = await business_resolution.resolve_item_return_route(
        QueueSession(
            execute_rows=[[
                board("M1", "beijing"),
                board("M1", "tianjin"),
            ]]
        ),
        ticket=ticket,
        item=item,
    )

    assert result["status"] == "needs_manual"
    assert result["message"] == "BOARD_CODE_ROUTE_CONFLICT"


@pytest.mark.anyio
async def test_policy_resolution_confirms_customer_and_snapshots_scope() -> None:
    ticket = RepairTicket(
        id=1,
        ticket_no="T1",
        customer_name="Acme （上海） 有限公司",
        request_date=date(2026, 7, 31),
    )
    item = RepairTicketItem(
        id=2,
        ticket_id=1,
        line_no=1,
        sn="SN0001",
        sn_asset_id=3,
    )
    asset = SnAsset(
        id=3,
        sn="SN0001",
        customer_code="CM001",
        customer_name="Acme(上海)有限公司",
        material_code="MAT1",
        material_name="Material",
        asset_status="valid",
        warranty_end_date=date(2025, 1, 1),
    )
    policy = CustomerServicePolicy(
        id=4,
        policy_code="P1",
        customer_code="CM001",
        customer_name="Acme(上海)有限公司",
        policy_type="special_out_of_warranty",
        charge_status="chargeable",
        customer_scope="domestic",
        repair_price=Decimal("1200"),
        currency="CNY",
        tax_rate=Decimal("13"),
        shipping_fee_text="one-way charge/单次收费",
        enabled=True,
    )
    session = QueueSession(
        execute_rows=[[item], [policy]],
        get_values={(SnAsset, 3): asset},
    )

    result = await business_resolution.resolve_and_snapshot_ticket_policy(
        session,
        ticket=ticket,
    )

    assert result["status"] == "resolved"
    assert ticket.customer_code == "CM001"
    assert ticket.customer_scope == "domestic"
    assert ticket.charge_status == "chargeable"
    assert ticket.policy_snapshot["policy_id"] == 4


@pytest.mark.anyio
async def test_unresolved_policy_clears_stale_decision_and_marks_manual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_task(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        business_resolution,
        "create_manual_task_if_missing",
        no_task,
    )
    ticket = RepairTicket(
        id=1,
        ticket_no="T1",
        customer_name="Acme",
        customer_scope="domestic",
        customer_scope_source="customer_policy",
        charge_status="free",
        charge_status_source="customer_policy",
        service_policy_id=99,
        policy_resolution_status="resolved",
    )
    item = RepairTicketItem(
        id=2,
        ticket_id=1,
        line_no=1,
        sn="SN0001",
        sn_asset_id=3,
    )
    asset = SnAsset(
        id=3,
        sn="SN0001",
        customer_code="CM001",
        customer_name="Acme",
        material_code="MAT1",
        material_name="Material",
        asset_status="valid",
    )

    result = await business_resolution.resolve_and_snapshot_ticket_policy(
        QueueSession(
            execute_rows=[[item], []],
            get_values={(SnAsset, 3): asset},
        ),
        ticket=ticket,
    )

    assert result["status"] == "missing"
    assert ticket.customer_code == "CM001"
    assert ticket.customer_scope is None
    assert ticket.charge_status == "manual_confirmation"
    assert ticket.service_policy_id is None
    assert ticket.policy_resolution_status == "needs_manual"


@pytest.mark.anyio
async def test_sap_export_uses_customer_mailing_fields_and_keeps_return_snapshot() -> None:
    ticket = RepairTicket(
        id=1,
        ticket_no="T1",
        version=2,
        customer_code="CM001",
        customer_name="Acme",
        customer_scope="domestic",
        charge_status="chargeable",
        policy_resolution_status="resolved",
        policy_snapshot={
            "policy_id": 4,
            "charge_status": "chargeable",
            "currency": "CNY",
            "repair_price": "1200",
            "tax_rate": "13",
            "shipping_fee_text": "one-way charge/单次收费",
        },
        mailing_address="Customer mailing address",
        contact_person="Customer Contact",
        contact_phone="13800000000",
        request_date=date(2026, 7, 31),
    )
    item = RepairTicketItem(
        id=2,
        ticket_id=1,
        line_no=1,
        sn="SN0001",
        material_code="MAT1",
        material_name="Material",
        return_location="tianjin",
        return_address="Repair return address",
        return_contact="Repair Receiver",
        return_phone="022-1",
        return_route_status="resolved",
        return_route_source="domestic_board_match",
        return_route_snapshot={"status": "resolved"},
    )
    export = TicketRelayExport(
        id=3,
        ticket_id=1,
        ticket_version=2,
        payload_hash="a" * 64,
        payload_snapshot={},
    )
    session = QueueSession(execute_rows=[[], [item]])

    rows = await sap_rma.ensure_export_lines(
        session,
        export=export,
        ticket=ticket,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.mailing_address == "Customer mailing address"
    assert row.contact_person == "Customer Contact"
    assert row.contact_phone == "13800000000"
    assert row.charge_status == "chargeable"
    assert row.policy_snapshot["shipping_address"] == "Repair return address"


@pytest.mark.parametrize(
    ("policy_type", "expected"),
    [
        ("permanent_free", "free"),
        ("annual_free", "annual_contract"),
        ("special_out_of_warranty", "chargeable"),
        ("unknown", "manual_confirmation"),
    ],
)
def test_policy_type_maps_to_explicit_charge_status(
    policy_type: str,
    expected: str,
) -> None:
    assert customer_policies.charge_status_for_policy_type(policy_type) == expected


@pytest.mark.anyio
async def test_manual_charge_status_blocks_sap_export() -> None:
    ticket = RepairTicket(
        id=1,
        ticket_no="T1",
        version=2,
        customer_code="CM001",
        customer_name="Acme",
        customer_scope="domestic",
        charge_status="manual_confirmation",
        policy_resolution_status="needs_manual",
        policy_snapshot={},
        mailing_address="Customer mailing address",
        contact_person="Customer Contact",
        contact_phone="13800000000",
    )
    item = RepairTicketItem(
        id=2,
        ticket_id=1,
        line_no=1,
        sn="SN0001",
        material_code="MAT1",
        material_name="Material",
    )
    export = TicketRelayExport(
        id=3,
        ticket_id=1,
        ticket_version=2,
        payload_hash="a" * 64,
        payload_snapshot={},
    )

    with pytest.raises(ValueError, match="SAP_EXPORT_REQUIRED_FIELDS_MISSING"):
        await sap_rma.ensure_export_lines(
            QueueSession(execute_rows=[[], [item]]),
            export=export,
            ticket=ticket,
        )
