from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Email, RepairTicket, ReplyRecord
from app.services.audit import log_operation
from app.services.common import utcnow
from app.services.mail_safety import TEST_MAIL_RECIPIENT
from app.services.replies import (
    _message_id_chain,
    _render_reply_templates,
    _require_reply_parent,
    _select_template,
    _send_reply_record,
)
from app.services.workflow import create_manual_task_if_missing


ACTIVE_ACK_STATUSES = {
    "pending_review",
    "approved_pending_send",
    "sending",
    "auto_sending",
    "sent",
    "send_uncertain",
    "send_failed",
}


async def confirm_device_received(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_id: int | None,
    source: str,
    source_email_id: int | None = None,
    note: str | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    ticket = await session.get(RepairTicket, ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")

    if ticket.device_received_at is None:
        ticket.device_received_at = utcnow()
        ticket.device_received_source = source
        ticket.device_received_email_id = source_email_id
        ticket.device_received_note = note
        ticket.device_received_idempotency_key = idempotency_key
    elif note and not ticket.device_received_note:
        ticket.device_received_note = note

    existing = await session.scalar(
        select(ReplyRecord)
        .where(
            ReplyRecord.ticket_id == ticket.id,
            ReplyRecord.reply_type == "device_received_ack",
            ReplyRecord.send_status.in_(ACTIVE_ACK_STATUSES),
        )
        .order_by(ReplyRecord.id.desc())
    )
    if existing is not None:
        return {
            "ticket_id": ticket.id,
            "status": ticket.device_receipt_ack_status,
            "reply_id": existing.id,
            "idempotent_reuse": True,
        }

    if ticket.current_status_code != "rma_sent" or ticket.rma_status != "sent":
        ticket.device_receipt_ack_status = "pending_prerequisite"
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="device_received_prerequisite",
            trigger_reason="公司已收货，但工单尚未通过完整安全校验或 RMA 尚未实际发送。",
            priority="high",
            email_id=source_email_id,
        )
        await log_operation(
            session,
            user_id=user_id,
            operation_type="device_received_deferred",
            target_type="repair_ticket",
            target_id=ticket.id,
            email_id=source_email_id,
            ticket_id=ticket.id,
            after_data={"ticket_status": ticket.current_status_code, "rma_status": ticket.rma_status},
        )
        return {"ticket_id": ticket.id, "status": "pending_prerequisite", "reply_id": None, "idempotent_reuse": False}

    try:
        related_email = await _require_reply_parent(
            session,
            ticket=ticket,
            related_email_id=source_email_id,
        )
    except HTTPException as exc:
        ticket.device_receipt_ack_status = "pending_review"
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="device_received_reply_parent_required",
            trigger_reason=str(exc.detail),
            priority="high",
            email_id=source_email_id,
        )
        return {
            "ticket_id": ticket.id,
            "status": "pending_review",
            "reply_id": None,
            "idempotent_reuse": False,
            "error_code": str(exc.detail),
        }
    language = "en-US" if ticket.language_code == "en-US" else "zh-CN"
    template = await _select_template(session, "device_received_ack", language)
    if template is None:
        ticket.device_receipt_ack_status = "pending_review"
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="device_received_reply_template_missing",
            trigger_reason=f"REPLY_TEMPLATE_NOT_FOUND:device_received_ack:{language}",
            priority="high",
            email_id=related_email.id,
        )
        return {
            "ticket_id": ticket.id,
            "status": "pending_review",
            "reply_id": None,
            "idempotent_reuse": False,
            "error_code": "REPLY_TEMPLATE_NOT_FOUND",
        }
    try:
        subject, body, base_template = await _render_reply_templates(
            session,
            content_template=template,
            ticket=ticket,
            missing_fields=None,
            parent=related_email,
        )
    except HTTPException as exc:
        ticket.device_receipt_ack_status = "pending_review"
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="device_received_reply_base_template_missing",
            trigger_reason=str(exc.detail),
            priority="high",
            email_id=related_email.id,
        )
        return {
            "ticket_id": ticket.id,
            "status": "pending_review",
            "reply_id": None,
            "idempotent_reuse": False,
            "error_code": str(exc.detail),
        }
    reply = ReplyRecord(
        ticket_id=ticket.id,
        related_email_id=related_email.id,
        template_id=template.id,
        base_template_id=base_template.id if base_template else None,
        reply_type="device_received_ack",
        followup_round=ticket.followup_count or 0,
        to_addresses=TEST_MAIL_RECIPIENT,
        cc_addresses=None,
        subject=subject,
        draft_body=body,
        final_body=body,
        generate_source="template",
        review_status="auto_approved" if settings.AUTO_SEND_ENABLED else "pending",
        reviewed_at=utcnow() if settings.AUTO_SEND_ENABLED else None,
        send_status="approved_pending_send" if settings.AUTO_SEND_ENABLED else "pending_review",
        in_reply_to=_message_id_chain(related_email.message_id),
        references_header=_message_id_chain(
            related_email.references_header,
            related_email.in_reply_to,
            related_email.message_id,
        ),
    )
    session.add(reply)
    await session.flush()

    if settings.AUTO_SEND_ENABLED:
        ticket.device_receipt_ack_status = "sending"
        await _send_reply_record(session, reply=reply, user_id=user_id, auto=True)
    else:
        ticket.device_receipt_ack_status = "pending_review"
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="device_received_ack_review",
            trigger_reason="公司已收货，普通回复自动发送关闭，需要人工审核收货确认邮件。",
            priority="high",
            email_id=source_email_id,
        )

    await log_operation(
        session,
        user_id=user_id,
        operation_type="device_received_confirmed",
        target_type="repair_ticket",
        target_id=ticket.id,
        email_id=source_email_id,
        ticket_id=ticket.id,
        after_data={"source": source, "reply_id": reply.id, "ack_status": ticket.device_receipt_ack_status},
    )
    return {
        "ticket_id": ticket.id,
        "status": ticket.device_receipt_ack_status,
        "reply_id": reply.id,
        "idempotent_reuse": False,
    }
