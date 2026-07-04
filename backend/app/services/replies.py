from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Email, ReplyRecord, ReplyTemplate, RepairTicket
from app.services.ai import generate_ai_reply_draft
from app.services.audit import log_operation
from app.services.common import model_to_dict, utcnow
from app.services.tickets import get_ticket
from app.services.workflow import create_manual_task_if_missing, transition_ticket

REPLY_FIELDS = (
    "id",
    "ticket_id",
    "related_email_id",
    "outgoing_email_id",
    "template_id",
    "reply_type",
    "followup_round",
    "missing_fields",
    "to_addresses",
    "cc_addresses",
    "subject",
    "draft_body",
    "final_body",
    "generate_source",
    "ai_call_log_id",
    "review_status",
    "reviewed_by_user_id",
    "reviewed_at",
    "send_status",
    "smtp_message_id",
    "in_reply_to",
    "references_header",
    "sent_at",
    "error_message",
    "created_at",
    "updated_at",
)


def serialize_reply(reply: ReplyRecord) -> dict[str, Any]:
    return model_to_dict(reply, REPLY_FIELDS)


def _missing_fields_text(missing_fields: dict[str, Any] | None) -> str:
    if not missing_fields:
        return "- 报修设备 SN\n- 故障描述\n- 联系方式"
    return "\n".join(f"- {key}: {value}" for key, value in missing_fields.items())


def _render_template(template: str, *, ticket: RepairTicket, missing_fields: dict[str, Any] | None) -> str:
    return (
        template.replace("{{ ticket_no }}", ticket.ticket_no)
        .replace("{{ missing_fields }}", _missing_fields_text(missing_fields))
        .replace("{{ customer_name }}", ticket.customer_name or "")
    )


async def _select_template(session: AsyncSession, reply_type: str, language: str) -> ReplyTemplate | None:
    return await session.scalar(
        select(ReplyTemplate)
        .where(ReplyTemplate.template_type == reply_type, ReplyTemplate.language == language, ReplyTemplate.enabled == True)  # noqa: E712
        .order_by(ReplyTemplate.version.desc(), ReplyTemplate.id.desc())
    )


def _infer_reply_type(ticket: RepairTicket, requested: str | None) -> str:
    if requested:
        return requested
    missing_fields = ticket.missing_fields or {}
    if any("sn" in str(key).lower() for key in missing_fields):
        return "sn_invalid"
    if ticket.current_status_code == "manual_review":
        return "manual_review"
    return "missing_fields"


async def list_replies(
    session: AsyncSession,
    *,
    ticket_id: int | None = None,
    review_status: str | None = None,
    send_status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    from app.services.common import paginate_scalars

    statement = select(ReplyRecord)
    if ticket_id:
        statement = statement.where(ReplyRecord.ticket_id == ticket_id)
    if review_status:
        statement = statement.where(ReplyRecord.review_status == review_status)
    if send_status:
        statement = statement.where(ReplyRecord.send_status == send_status)
    statement = statement.order_by(ReplyRecord.created_at.desc(), ReplyRecord.id.desc())
    replies, total = await paginate_scalars(session, statement, page, page_size)
    return [serialize_reply(reply) for reply in replies], total


async def get_reply(session: AsyncSession, reply_id: int) -> ReplyRecord:
    reply = await session.get(ReplyRecord, reply_id)
    if reply is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="REPLY_NOT_FOUND")
    return reply


async def create_reply_draft(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_id: int | None,
    reply_type: str | None = None,
    related_email_id: int | None = None,
    language: str = "zh-CN",
    missing_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ticket = await get_ticket(session, ticket_id)
    if ticket.followup_count >= ticket.max_followup_count:
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="manual_review",
            trigger_event="manual_review_required",
            user_id=user_id,
            reason="追问次数已达到上限。",
        )
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="followup_limit",
            trigger_reason="追问次数已达到上限。",
            priority="high",
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="FOLLOWUP_LIMIT_EXCEEDED")

    reply_kind = _infer_reply_type(ticket, reply_type)
    template = await _select_template(session, reply_kind, language)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="REPLY_TEMPLATE_NOT_FOUND")

    related_email = await session.get(Email, related_email_id or ticket.source_email_id) if (related_email_id or ticket.source_email_id) else None
    effective_missing_fields = missing_fields if missing_fields is not None else ticket.missing_fields
    subject = _render_template(template.subject_template or f"请补充报修信息：{ticket.ticket_no}", ticket=ticket, missing_fields=effective_missing_fields)
    body = _render_template(template.body_template, ticket=ticket, missing_fields=effective_missing_fields)
    generate_source = "template"
    ai_call_log_id: int | None = None
    ai_draft = await generate_ai_reply_draft(
        session,
        ticket=ticket,
        related_email=related_email,
        reply_type=reply_kind,
        language=language,
        missing_fields=effective_missing_fields,
        template_subject=subject,
        template_body=body,
    )
    if ai_draft:
        subject = ai_draft["subject"]
        body = ai_draft["body"]
        effective_missing_fields = ai_draft.get("missing_fields") or effective_missing_fields
        ai_call_log_id = ai_draft["ai_call_log"].id
        generate_source = "ai"
    reply = ReplyRecord(
        ticket_id=ticket.id,
        related_email_id=related_email.id if related_email else None,
        template_id=template.id,
        reply_type=reply_kind,
        followup_round=ticket.followup_count + 1,
        missing_fields=effective_missing_fields,
        to_addresses=ticket.contact_email or (related_email.from_address if related_email else ""),
        cc_addresses=related_email.cc_addresses if related_email else None,
        subject=subject,
        draft_body=body,
        final_body=body,
        generate_source=generate_source,
        ai_call_log_id=ai_call_log_id,
        review_status="pending",
        send_status="draft",
        in_reply_to=related_email.message_id if related_email else None,
        references_header=related_email.references_header if related_email else None,
    )
    session.add(reply)
    await session.flush()
    await create_manual_task_if_missing(
        session,
        ticket=ticket,
        task_type="reply_review",
        trigger_reason="追问草稿需要人工审核。",
        email_id=related_email.id if related_email else None,
    )
    if ticket.current_status_code == "need_customer_info":
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="auto_replied",
            trigger_event="reply_draft_created",
            user_id=user_id,
            reason="已生成追问草稿，等待人工审核。",
            metadata={"reply_id": reply.id, "auto_send_enabled": settings.AUTO_SEND_ENABLED},
        )
    ticket.followup_count += 1
    await log_operation(
        session,
        user_id=user_id,
        operation_type="reply_draft_created",
        target_type="reply_record",
        target_id=reply.id,
        after_data={
            "ticket_id": ticket.id,
            "reply_type": reply_kind,
            "generate_source": generate_source,
            "ai_call_log_id": ai_call_log_id,
            "auto_send_enabled": settings.AUTO_SEND_ENABLED,
        },
    )
    return {"reply": serialize_reply(reply), "auto_send_enabled": settings.AUTO_SEND_ENABLED}


async def update_reply(session: AsyncSession, *, reply_id: int, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
    reply = await get_reply(session, reply_id)
    if reply.review_status == "approved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="REPLY_ALREADY_APPROVED")
    allowed = {"subject", "final_body", "to_addresses", "cc_addresses"}
    changed: dict[str, Any] = {}
    for field, value in values.items():
        if value is None:
            continue
        if field not in allowed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"REPLY_FIELD_NOT_ALLOWED:{field}")
        old_value = getattr(reply, field)
        if old_value == value:
            continue
        setattr(reply, field, value)
        changed[field] = {"old": old_value, "new": value}
    if changed:
        await log_operation(
            session,
            user_id=user_id,
            operation_type="reply_updated",
            target_type="reply_record",
            target_id=reply.id,
            after_data=changed,
        )
    return serialize_reply(reply)


async def approve_reply(session: AsyncSession, *, reply_id: int, user_id: int) -> dict[str, Any]:
    reply = await get_reply(session, reply_id)
    reply.review_status = "approved"
    reply.reviewed_by_user_id = user_id
    reply.reviewed_at = utcnow()
    if settings.AUTO_SEND_ENABLED:
        reply.send_status = "pending"
        reply.error_message = "SMTP 真实发送接入预留，当前未执行外发。"
    else:
        reply.send_status = "send_disabled"
        reply.error_message = "AUTO_SEND_ENABLED=false，已审核但未真实发送。"
    await log_operation(
        session,
        user_id=user_id,
        operation_type="reply_approved",
        target_type="reply_record",
        target_id=reply.id,
        after_data={"send_status": reply.send_status, "auto_send_enabled": settings.AUTO_SEND_ENABLED},
    )
    return {"reply": serialize_reply(reply), "auto_send_enabled": settings.AUTO_SEND_ENABLED}


async def reject_reply(session: AsyncSession, *, reply_id: int, user_id: int, reason: str) -> dict[str, Any]:
    reply = await get_reply(session, reply_id)
    reply.review_status = "rejected"
    reply.reviewed_by_user_id = user_id
    reply.reviewed_at = utcnow()
    reply.send_status = "rejected"
    reply.error_message = reason
    await log_operation(
        session,
        user_id=user_id,
        operation_type="reply_rejected",
        target_type="reply_record",
        target_id=reply.id,
        description=reason,
    )
    return serialize_reply(reply)
