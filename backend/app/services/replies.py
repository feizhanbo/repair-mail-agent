from __future__ import annotations

import hashlib
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Email, EmailTicketLink, ReplyRecord, ReplyTemplate, RepairTicket
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
    if ticket.missing_fields:
        return "missing_fields"
    return "receipt"


def _fallback_reply_content(reply_type: str, *, ticket: RepairTicket, missing_fields: dict[str, Any] | None) -> tuple[str, str]:
    if reply_type == "receipt":
        return (
            f"报修已受理：{ticket.ticket_no}",
            (
                f"您好，\n\n我们已收到您的报修邮件并生成工单 {ticket.ticket_no}。"
                "系统已完成初步信息核对，后续处理进展会继续通过邮件同步。\n\n谢谢。"
            ),
        )
    return (
        f"请补充报修信息：{ticket.ticket_no}",
        f"您好，\n\n我们已收到您的报修邮件，但还需要补充以下信息后才能继续处理：\n{_missing_fields_text(missing_fields)}\n\n请直接回复本邮件补充。谢谢。",
    )


def _valid_recipient(value: str | None) -> bool:
    parsed = parseaddr(value or "")[1]
    return bool(parsed and "@" in parsed and "." in parsed.rsplit("@", 1)[-1])


def _smtp_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD)


def _reply_can_auto_send(reply: ReplyRecord, *, confidence_score: float | None = None, risk_level: str | None = None) -> bool:
    if settings.REPLY_SEND_MODE != "auto_send" or not settings.AUTO_SEND_ENABLED:
        return False
    if not _valid_recipient(reply.to_addresses):
        return False
    if confidence_score is not None and confidence_score < settings.AUTO_SEND_MIN_CONFIDENCE:
        return False
    if risk_level and risk_level not in {"low", "normal"}:
        return False
    return True


def _send_reply_via_smtp(reply: ReplyRecord) -> tuple[bool, str | None, str | None]:
    if not _valid_recipient(reply.to_addresses):
        return False, None, "收件人邮箱无效，已转人工确认。"
    if not _smtp_configured():
        return False, None, "SMTP 未配置，邮件暂未发送。"
    message_id = make_msgid(domain=parseaddr(settings.SMTP_USER)[1].split("@")[-1] if "@" in settings.SMTP_USER else "repair.local")
    message = EmailMessage()
    message["From"] = settings.SMTP_USER
    message["To"] = reply.to_addresses
    if reply.cc_addresses:
        message["Cc"] = reply.cc_addresses
    message["Subject"] = reply.subject or ""
    message["Message-ID"] = message_id
    if reply.in_reply_to:
        message["In-Reply-To"] = reply.in_reply_to
    if reply.references_header:
        message["References"] = reply.references_header
    message.set_content(reply.final_body or reply.draft_body or "")
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as smtp:
            if settings.SMTP_PORT == 587:
                smtp.starttls()
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(message)
    except Exception:
        return False, None, "邮件发送失败，请检查邮箱发送配置后重试。"
    return True, message_id, None


async def _archive_outbound_email(
    session: AsyncSession,
    *,
    reply: ReplyRecord,
    ticket: RepairTicket,
    smtp_message_id: str,
) -> Email:
    existing = await session.scalar(select(Email).where(Email.message_id == smtp_message_id))
    if existing is not None:
        reply.outgoing_email_id = existing.id
        return existing
    body = reply.final_body or reply.draft_body or ""
    email = Email(
        thread_id=ticket.thread_id,
        mail_direction="outbound",
        mailbox_account=settings.SMTP_USER or "system",
        folder_name="sent",
        message_id=smtp_message_id,
        message_id_hash=hashlib.sha256(smtp_message_id.encode("utf-8")).hexdigest(),
        in_reply_to=reply.in_reply_to,
        references_header=reply.references_header,
        raw_headers={"generated_by": "reply_record", "reply_id": reply.id},
        from_address=settings.SMTP_USER or "system@repair.local",
        to_addresses=reply.to_addresses,
        cc_addresses=reply.cc_addresses,
        subject=reply.subject,
        normalized_subject=reply.subject,
        sent_at=reply.sent_at,
        received_at=reply.sent_at,
        text_body=body,
        clean_body=body,
        latest_reply_segment=body,
        parse_status="sent",
        intent_type="outbound_reply",
    )
    session.add(email)
    await session.flush()
    reply.outgoing_email_id = email.id
    linked = await session.scalar(
        select(EmailTicketLink).where(EmailTicketLink.email_id == email.id, EmailTicketLink.ticket_id == ticket.id, EmailTicketLink.link_type == "outbound")
    )
    if linked is None:
        session.add(EmailTicketLink(email_id=email.id, ticket_id=ticket.id, link_type="outbound", link_reason="reply sent"))
    return email


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


async def _send_reply_record(
    session: AsyncSession,
    *,
    reply: ReplyRecord,
    user_id: int | None,
    auto: bool,
) -> None:
    reply.send_status = "auto_sending" if auto else "sending"
    ok, smtp_message_id, error = _send_reply_via_smtp(reply)
    if ok:
        reply.send_status = "sent"
        reply.smtp_message_id = smtp_message_id
        reply.sent_at = utcnow()
        reply.error_message = None
        ticket = await get_ticket(session, reply.ticket_id)
        if smtp_message_id:
            await _archive_outbound_email(session, reply=reply, ticket=ticket, smtp_message_id=smtp_message_id)
    else:
        reply.send_status = "send_failed"
        reply.error_message = error
        ticket = await get_ticket(session, reply.ticket_id)
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="reply_send_failed",
            trigger_reason=error or "邮件发送失败，需要人工处理。",
            priority="high",
            email_id=reply.related_email_id,
            assigned_user_id=ticket.assigned_user_id,
        )
    await log_operation(
        session,
        user_id=user_id,
        operation_type="reply_sent" if ok else "reply_send_failed",
        target_type="reply_record",
        target_id=reply.id,
        after_data={"send_status": reply.send_status, "auto": auto, "smtp_message_id": smtp_message_id},
    )


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
    reply_kind = _infer_reply_type(ticket, reply_type)
    if reply_kind != "receipt" and ticket.followup_count >= ticket.max_followup_count:
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

    template = await _select_template(session, reply_kind, language)
    if template is None and reply_kind != "receipt":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="REPLY_TEMPLATE_NOT_FOUND")

    related_email = await session.get(Email, related_email_id or ticket.source_email_id) if (related_email_id or ticket.source_email_id) else None
    effective_missing_fields = missing_fields if missing_fields is not None else ticket.missing_fields
    if template is None:
        fallback_subject, fallback_body = _fallback_reply_content(reply_kind, ticket=ticket, missing_fields=effective_missing_fields)
        template = SimpleNamespace(id=None, subject_template=fallback_subject, body_template=fallback_body)
    subject = _render_template(template.subject_template or f"请补充报修信息：{ticket.ticket_no}", ticket=ticket, missing_fields=effective_missing_fields)
    body = _render_template(template.body_template, ticket=ticket, missing_fields=effective_missing_fields)
    generate_source = "template"
    ai_call_log_id: int | None = None
    reply_confidence_score: float | None = None
    reply_risk_level: str | None = None
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
        reply_confidence_score = ai_draft.get("confidence_score")
        reply_risk_level = ai_draft.get("risk_level")
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
        send_status="pending_review",
        in_reply_to=related_email.message_id if related_email else None,
        references_header=related_email.references_header if related_email else None,
    )
    session.add(reply)
    await session.flush()
    if not _reply_can_auto_send(reply, confidence_score=reply_confidence_score, risk_level=reply_risk_level):
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="reply_review",
            trigger_reason="回复草稿需要人工确认后发送。",
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
            metadata={"reply_id": reply.id, "auto_send_enabled": settings.AUTO_SEND_ENABLED, "reply_send_mode": settings.REPLY_SEND_MODE},
        )
    if reply_kind != "receipt":
        ticket.followup_count += 1
    if _reply_can_auto_send(reply, confidence_score=reply_confidence_score, risk_level=reply_risk_level):
        reply.review_status = "auto_approved"
        reply.reviewed_at = utcnow()
        await _send_reply_record(session, reply=reply, user_id=user_id, auto=True)
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
            "reply_send_mode": settings.REPLY_SEND_MODE,
        },
    )
    return {"reply": serialize_reply(reply), "auto_send_enabled": settings.AUTO_SEND_ENABLED, "reply_send_mode": settings.REPLY_SEND_MODE}


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
    reply.send_status = "approved_pending_send"
    reply.error_message = None
    await _send_reply_record(session, reply=reply, user_id=user_id, auto=False)
    await log_operation(
        session,
        user_id=user_id,
        operation_type="reply_approved",
        target_type="reply_record",
        target_id=reply.id,
        after_data={
            "send_status": reply.send_status,
            "auto_send_enabled": settings.AUTO_SEND_ENABLED,
            "reply_send_mode": settings.REPLY_SEND_MODE,
        },
    )
    return {"reply": serialize_reply(reply), "auto_send_enabled": settings.AUTO_SEND_ENABLED, "reply_send_mode": settings.REPLY_SEND_MODE}


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
