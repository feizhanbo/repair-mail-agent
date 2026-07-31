from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1 import notifications as notification_api
from app.models import (
    Email,
    EmailAttachment,
    ManualReviewTask,
    OssObject,
    RepairTicket,
    ReplyRecord,
    TicketRma,
    User,
)
from app.schemas.business import ManualTaskResolveRequest
from app.services import notifications, workflow


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Result:
    def scalars(self):
        return self

    def first(self):
        return None


class Session:
    def __init__(self, owner: User | None) -> None:
        self.owner = owner
        self.added = []

    async def execute(self, _statement):
        return Result()

    async def scalar(self, _statement):
        return self.owner

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = len(self.added)


@pytest.mark.anyio
async def test_manual_task_is_pending_with_system_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session(User(id=11, username="miya", status="active"))
    notices: list[dict] = []

    async def fake_notification(_session, **kwargs):
        notices.append(kwargs)

    monkeypatch.setattr(workflow, "create_notification", fake_notification)
    ticket = SimpleNamespace(id=8, ticket_no="RMA-TEST", source_email_id=3, assigned_user_id=11)

    task = await workflow.create_manual_task_if_missing(session, ticket=ticket, task_type="manual")

    assert isinstance(task, ManualReviewTask)
    assert task.status == "pending"
    assert task.assigned_user_id == 11
    assert task.claimed_by_user_id is None
    assert notices[0]["event_type"] == "manual_review_assigned"
    assert notices[0]["recipient_user_id"] == 11


@pytest.mark.anyio
async def test_missing_required_operator_stays_visible_and_notifies_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session(None)
    notices: list[dict] = []

    async def fake_notification(_session, **kwargs):
        notices.append(kwargs)

    monkeypatch.setattr(workflow, "create_notification", fake_notification)
    ticket = SimpleNamespace(id=9, ticket_no="RMA-FAILED", source_email_id=4, assigned_user_id=None)

    task = await workflow.create_manual_task_if_missing(session, ticket=ticket, task_type="manual")

    assert task.status == "pending"
    assert task.assigned_user_id is None
    assert notices[0]["event_type"] == "manual_review_assignment_failed"
    assert notices[0]["recipient_role_code"] == "admin"


@pytest.mark.anyio
async def test_resolved_notification_cannot_be_changed_back_to_read(monkeypatch: pytest.MonkeyPatch) -> None:
    event = SimpleNamespace(id=7)
    state = SimpleNamespace(status="resolved", read_at=None)

    async def fake_get(*_args, **_kwargs):
        return event, state

    monkeypatch.setattr(notifications, "get_user_notification", fake_get)
    returned = await notifications.mark_user_notification_read(SimpleNamespace(), notification_id=7, user_id=2)

    assert returned == (event, state)
    assert state.status == "resolved"
    assert state.read_at is not None


@pytest.mark.anyio
async def test_missing_user_notification_state_returns_explicit_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(notification_api, "mark_user_notification_read", missing)
    with pytest.raises(HTTPException) as exc_info:
        await notification_api.read_notification(7, SimpleNamespace(), SimpleNamespace(id=2))

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "NOTIFICATION_NOT_FOUND"


@pytest.mark.anyio
async def test_manual_review_cannot_leave_while_another_task_is_open() -> None:
    class BlockingSession:
        async def scalar(self, _statement):
            return 42

    ticket = SimpleNamespace(id=8, current_status_code="manual_review")
    with pytest.raises(HTTPException) as exc_info:
        await workflow.transition_ticket(
            BlockingSession(),
            ticket=ticket,
            to_status_code="need_customer_info",
            trigger_event="manual_resolved",
            resolving_task_id=7,
        )
    assert exc_info.value.detail == "MANUAL_TASKS_UNRESOLVED"
    assert ticket.current_status_code == "manual_review"


@pytest.mark.anyio
async def test_return_route_task_does_not_block_sap_ready_transition() -> None:
    class RouteOnlySession:
        def __init__(self) -> None:
            self.calls = 0
            self.added = []

        async def scalar(self, statement):
            self.calls += 1
            if self.calls == 1:
                assert "manual_review_tasks.task_type !=" in str(statement)
                return None
            if self.calls == 2:
                return SimpleNamespace(is_terminal=False)
            return SimpleNamespace(condition_desc="validated")

        def add(self, value) -> None:
            self.added.append(value)

    ticket = SimpleNamespace(
        id=8,
        current_status_code="manual_review",
        version=3,
    )
    session = RouteOnlySession()

    await workflow.transition_ticket(
        session,
        ticket=ticket,
        to_status_code="ready_for_export",
        trigger_event="manual_resolved",
        resolving_task_id=7,
        metadata={"safety_check_hash": "a" * 64},
    )

    assert ticket.current_status_code == "ready_for_export"
    assert ticket.version == 4


@pytest.mark.anyio
async def test_rma_sent_and_closed_cannot_bypass_evidence_gates() -> None:
    unused_session = SimpleNamespace()
    ready = SimpleNamespace(
        id=1,
        current_status_code="ready_for_export",
    )
    with pytest.raises(HTTPException) as sent_error:
        await workflow.transition_ticket(
            unused_session,
            ticket=ready,
            to_status_code="rma_sent",
            trigger_event="rma_reply_sent",
            metadata={"reply_id": 2},
        )
    assert sent_error.value.detail == "RMA_SMTP_EVIDENCE_REQUIRED"

    sent = SimpleNamespace(
        id=1,
        current_status_code="rma_sent",
    )
    with pytest.raises(HTTPException) as close_error:
        await workflow.transition_ticket(
            unused_session,
            ticket=sent,
            to_status_code="closed",
            trigger_event="rma_issued_and_archived",
            metadata={"closure_gates": {"smtp_sent": True}},
        )
    assert close_error.value.detail == "RMA_ISSUE_CLOSURE_GATES_REQUIRED"


def test_manual_task_schema_rejects_legacy_close_action() -> None:
    with pytest.raises(ValidationError):
        ManualTaskResolveRequest(
            resolution="legacy direct close must not be accepted",
            next_action="close_ticket",
        )


@pytest.mark.anyio
async def test_rma_closure_rechecks_durable_database_facts() -> None:
    now = workflow.utcnow()
    ticket = RepairTicket(id=1, ticket_no="T-1", current_status_code="rma_sent")
    reply = ReplyRecord(
        id=2,
        ticket_id=1,
        reply_type="rma_authorization",
        to_addresses="rmatest2@accotest.com",
        send_status="sent",
        smtp_message_id="<issued@accotest.com>",
        outgoing_email_id=3,
        rma_pdf_oss_object_id=4,
        archive_status="archived",
        archive_verified_at=now,
    )
    rma = TicketRma(
        id=5,
        ticket_id=1,
        rma_no="2026070910",
        status="issued",
        reply_record_id=2,
        received_at=now,
        pdf_oss_object_id=4,
        pdf_sha256="a" * 64,
        pdf_validation_status="passed",
        pdf_archive_status="archived",
        pdf_archived_at=now,
        issued_at=now,
    )
    outgoing = Email(
        id=3,
        mailbox_account="rmatest1@accotest.com",
        mail_direction="outbound",
        message_id="<issued@accotest.com>",
        from_address="rmatest1@accotest.com",
        raw_eml_oss_object_id=6,
    )
    pdf_object = OssObject(id=4, object_key="rma.pdf", original_file_name="rma.pdf")
    attachment = EmailAttachment(
        id=7,
        email_id=3,
        oss_object_id=4,
        file_name="rma.pdf",
        file_hash="a" * 64,
    )

    async def get(model, object_id):
        return {
            (ReplyRecord, 2): reply,
            (Email, 3): outgoing,
            (OssObject, 4): pdf_object,
        }.get((model, object_id))

    session = SimpleNamespace(
        get=get,
        scalar=AsyncMock(side_effect=[rma, attachment]),
    )
    missing = await workflow._rma_closure_missing_facts(
        session,
        ticket=ticket,
        metadata={"reply_id": 2},
    )
    assert missing == []

    outgoing.raw_eml_oss_object_id = None
    session.scalar = AsyncMock(side_effect=[rma, attachment])
    missing = await workflow._rma_closure_missing_facts(
        session,
        ticket=ticket,
        metadata={"reply_id": 2},
    )
    assert "outbound_eml_archived" in missing
