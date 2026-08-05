from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.models import (
    Email,
    EmailAttachment,
    OssObject,
    RepairTicket,
    ReplyRecord,
    TicketRma,
)
from app.services import jobs, replies


@pytest.mark.anyio
async def test_rma_issue_closes_only_after_all_archive_gates_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = RepairTicket(
        id=1,
        ticket_no="T-1",
        current_status_code="rma_sent",
    )
    reply = ReplyRecord(
        id=2,
        ticket_id=1,
        reply_type="rma_authorization",
        to_addresses="rmatest2@accotest.com",
        send_status="sent",
        smtp_message_id="<rma@accotest.com>",
        outgoing_email_id=3,
        rma_pdf_oss_object_id=4,
        archive_attempt_count=0,
        rma_pdf_data_snapshot={"pdf_sha256": "a" * 64},
    )
    rma = TicketRma(
        id=5,
        ticket_id=1,
        rma_no="2026070910",
        pdf_oss_object_id=4,
    )
    outgoing = Email(
        id=3,
        mailbox_account="rmatest1@accotest.com",
        message_id="<rma@accotest.com>",
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

    async def get(model, object_id, **_kwargs):
        if model is Email and object_id == 3:
            return outgoing
        if model is OssObject and object_id == 4:
            return pdf_object
        return None

    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[rma, attachment]),
        get=get,
    )

    async def transition(_session, *, ticket, trigger_event, **_kwargs):
        assert trigger_event == "rma_issued_and_archived"
        ticket.current_status_code = "closed"
        return ticket

    monkeypatch.setattr(replies, "transition_ticket", transition)
    monkeypatch.setattr(replies, "_ensure_reply_manual_task", AsyncMock())

    closed = await replies._finalize_rma_issue(
        session,
        ticket=ticket,
        reply=reply,
        user_id=8,
        auto=True,
    )

    assert closed is True
    assert ticket.current_status_code == "closed"
    assert ticket.rma_status == "issued"
    assert reply.archive_status == "archived"
    assert reply.archive_verified_at is not None
    assert rma.status == "issued"
    assert rma.pdf_validation_status == "passed"
    assert rma.pdf_archive_status == "archived"
    assert rma.issued_at is not None


@pytest.mark.anyio
async def test_archive_retry_never_calls_smtp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = RepairTicket(id=1, ticket_no="T-1", current_status_code="rma_sent")
    reply = ReplyRecord(
        id=2,
        ticket_id=1,
        reply_type="rma_authorization",
        to_addresses="rmatest2@accotest.com",
        send_status="sent",
        smtp_message_id="<accepted@accotest.com>",
        outgoing_email_id=3,
        rma_pdf_oss_object_id=4,
        archive_attempt_count=1,
    )

    async def get(model, _object_id, **_kwargs):
        if model is ReplyRecord:
            return reply
        if model is RepairTicket:
            return ticket
        return None

    session = SimpleNamespace(get=get)
    monkeypatch.setattr(
        replies,
        "start_external_operation",
        AsyncMock(return_value=SimpleNamespace(status="running")),
    )

    async def finalize(*_args, **_kwargs):
        ticket.current_status_code = "closed"
        return True

    monkeypatch.setattr(replies, "_finalize_rma_issue", finalize)
    monkeypatch.setattr(
        replies,
        "_send_reply_via_smtp",
        Mock(side_effect=AssertionError("archive retry must never call SMTP")),
    )

    result = await replies.retry_rma_archive(
        session,
        reply_id=reply.id,
        user_id=9,
    )

    assert result["status"] == "closed"
    assert result["idempotent_reuse"] is False
    replies._send_reply_via_smtp.assert_not_called()


@pytest.mark.anyio
async def test_closed_archive_retry_normalizes_ticket_rma_status_without_smtp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = RepairTicket(
        id=1,
        ticket_no="T-1",
        current_status_code="closed",
        rma_status="sent",
    )
    reply = ReplyRecord(
        id=2,
        ticket_id=1,
        reply_type="rma_authorization",
        to_addresses="rmatest2@accotest.com",
        send_status="sent",
    )
    rma = TicketRma(
        id=3,
        ticket_id=1,
        rma_no="2026070910",
        status="issued",
        issued_at=replies.utcnow(),
    )

    async def get(model, _object_id, **_kwargs):
        return reply if model is ReplyRecord else ticket if model is RepairTicket else None

    session = SimpleNamespace(get=get, scalar=AsyncMock(return_value=rma))
    monkeypatch.setattr(
        replies,
        "_send_reply_via_smtp",
        Mock(side_effect=AssertionError("closed archive retry must never call SMTP")),
    )

    result = await replies.retry_rma_archive(
        session,
        reply_id=reply.id,
        user_id=8,
    )

    assert result["idempotent_reuse"] is True
    assert ticket.rma_status == "issued"


@pytest.mark.anyio
async def test_failed_post_smtp_archive_queues_one_archive_only_recovery_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = RepairTicket(id=1, ticket_no="T-1", current_status_code="rma_sent")
    reply = ReplyRecord(
        id=2,
        ticket_id=1,
        reply_type="rma_authorization",
        to_addresses="rmatest2@accotest.com",
        send_status="sent",
    )
    enqueue = AsyncMock(return_value=SimpleNamespace(id=10))
    monkeypatch.setattr(jobs, "enqueue_job", enqueue)

    await replies._enqueue_rma_archive_retry(
        SimpleNamespace(),
        ticket=ticket,
        reply=reply,
        user_id=9,
    )

    assert reply.next_retry_at is not None
    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert kwargs["job_type"] == "rma_archive"
    assert kwargs["resource_id"] == reply.id
    assert kwargs["idempotency_key"] == "rma_archive:2"
    assert kwargs["max_attempts"] == 3
