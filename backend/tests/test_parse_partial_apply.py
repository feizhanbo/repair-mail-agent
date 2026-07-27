from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models import Email, FieldAuditLog, ParseResult, RepairTicket, RepairTicketItem
from app.services.tickets import _create_items_from_parse_result, apply_parse_result


class ScalarRows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.values[0] if self.values else None


class ItemSession:
    def __init__(self):
        self.execute_count = 0
        self.added = []

    async def execute(self, _statement):
        self.execute_count += 1
        return ScalarRows([] if self.execute_count == 1 else [0])

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for value in self.added:
            if isinstance(value, RepairTicketItem) and value.id is None:
                value.id = 100 + len([item for item in self.added if isinstance(item, RepairTicketItem)])

    async def delete(self, _value):
        return None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_partial_item_selection_creates_only_selected_candidate() -> None:
    session = ItemSession()
    ticket = RepairTicket(id=1, ticket_no="RMATEST", problem_description="Synthetic")
    parse = ParseResult(
        id=2,
        email_id=3,
        extracted_items={
            "items": [
                {"sn": "TESTSN00000001", "material_code": "PART-A"},
                {"sn": "TESTSN00000002", "material_code": "PART-B"},
            ]
        },
    )

    await _create_items_from_parse_result(
        session,
        ticket,
        parse,
        user_id=7,
        selected_item_indices={1},
    )

    items = [value for value in session.added if isinstance(value, RepairTicketItem)]
    audits = [value for value in session.added if isinstance(value, FieldAuditLog)]
    assert [item.sn for item in items] == ["TESTSN00000002"]
    assert len(audits) == 1


@pytest.mark.anyio
async def test_reparse_reconciles_existing_placeholder_without_duplicate_line() -> None:
    placeholders = [
        RepairTicketItem(id=11, ticket_id=1, line_no=1, sn=None, quantity=1, failure_description="Controlled failure"),
        RepairTicketItem(id=12, ticket_id=1, line_no=2, sn=None, quantity=1, failure_description="Controlled failure"),
    ]

    class ReconcileSession(ItemSession):
        def __init__(self):
            super().__init__()
            self.deleted = []

        async def execute(self, _statement):
            return ScalarRows(placeholders)

        async def delete(self, value):
            self.deleted.append(value)

    session = ReconcileSession()
    ticket = RepairTicket(id=1, ticket_no="RMATEST", problem_description="Controlled failure")
    parse = ParseResult(
        id=2,
        email_id=3,
        extracted_items={
            "items": [
                {"line_no": 1, "sn": "M8123260108000171", "failure_description": "Controlled failure"},
            ]
        },
    )

    await _create_items_from_parse_result(session, ticket, parse, user_id=7)

    assert placeholders[0].sn == "M8123260108000171"
    assert session.deleted == [placeholders[1]]
    assert not [value for value in session.added if isinstance(value, RepairTicketItem)]


@pytest.mark.anyio
async def test_customer_supplement_explicit_sn_correction_replaces_unlocked_old_items() -> None:
    old_items = [
        RepairTicketItem(id=11, ticket_id=1, line_no=1, sn="OLD-SN-1", quantity=1),
        RepairTicketItem(id=12, ticket_id=1, line_no=2, sn="OLD-SN-2", quantity=1),
    ]
    source_email = Email(
        id=3,
        mailbox_account="test",
        message_id="<supplement@example.test>",
        from_address="customer@example.test",
        latest_reply_segment="原两个SN录入有误并更正为 NEW-SN-1、NEW-SN-2。",
    )

    class CorrectionSession(ItemSession):
        def __init__(self):
            super().__init__()
            self.deleted = []

        async def execute(self, _statement):
            return ScalarRows(old_items)

        async def get(self, model, identity):
            assert model is Email and identity == 3
            return source_email

        async def delete(self, value):
            self.deleted.append(value)

    session = CorrectionSession()
    ticket = RepairTicket(id=1, ticket_no="RMATEST", problem_description="Controlled failure")
    parse = ParseResult(
        id=2,
        email_id=3,
        intent_type="customer_supplement",
        extracted_items={
            "items": [
                {"line_no": 1, "sn": "NEW-SN-1"},
                {"line_no": 2, "sn": "NEW-SN-2"},
            ]
        },
    )

    await _create_items_from_parse_result(session, ticket, parse, user_id=None)

    assert session.deleted == old_items
    created = [value for value in session.added if isinstance(value, RepairTicketItem)]
    assert [item.sn for item in created] == ["NEW-SN-1", "NEW-SN-2"]


@pytest.mark.anyio
async def test_partial_apply_requires_an_explicit_field_or_item_selection() -> None:
    parse = ParseResult(id=2, email_id=3)

    class Session:
        async def get(self, model, identity):
            assert model is ParseResult and identity == 2
            return parse

    with pytest.raises(HTTPException) as exc:
        await apply_parse_result(Session(), parse_result_id=2, action="partial_apply")

    assert exc.value.status_code == 400
    assert exc.value.detail == "PARSE_RESULT_PARTIAL_SELECTION_REQUIRED"
