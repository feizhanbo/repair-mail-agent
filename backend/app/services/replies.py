from __future__ import annotations

import asyncio
import hashlib
import logging
import smtplib
from datetime import date
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parseaddr
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.request_context import get_correlation_id
from app.models import Email, EmailAttachment, EmailTicketLink, OssObject, ReplyRecord, ReplyTemplate, RepairTicket
from app.services.ai import generate_ai_reply_draft
from app.services.audit import log_operation, log_system_event
from app.services.business_rules import is_followup_reply_type
from app.services.common import model_to_dict, utcnow
from app.services.mail_safety import TEST_MAIL_RECIPIENT, TEST_MAIL_SENDER, test_envelope_allowed, test_only_subject
from app.services.rma_pdf import (
    RmaPdfError,
    TEMPLATE_VERSION as RMA_TEMPLATE_VERSION,
    build_rma_pdf_data,
    normalize_rma_template_version,
    render_rma_pdf,
    rma_pdf_file_name,
    rma_pdf_snapshot,
)
from app.services.storage import StorageConfigurationError, StorageUploadError, download_oss_object_bytes, upload_bytes_to_oss
from app.services.tickets import get_ticket
from app.services.workflow import create_manual_task_if_missing, transition_ticket

logger = logging.getLogger(__name__)
_smtp_semaphore = asyncio.Semaphore(max(1, settings.MAIL_IO_CONCURRENCY))

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
    "rma_pdf_oss_object_id",
    "reply_template_version",
    "rma_template_version",
    "rma_pdf_data_snapshot",
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

RMA_ZH_SUBJECT = "RMA维修授权：{{ ticket_no }} {{ customer_name }}"
RMA_REPLY_ZH_VERSION = "rma_reply_zh_v1"
RMA_ZH_BODY = """您好：

RMA维修授权表见附件。
为了不耽误贵司维修进度，请注意以下事项：
1. 请务必打印 RMA 表，并与报修板一同寄出。
2. 请妥善包装设备，并核对 RMA 表中的返回地址；如需变更地址请提前告知。
3. 维修工期预计为 10 个工作日，实际进度以维修检测结果为准。

谢谢。"""

OVERSEAS_WARRANTY_IN_VERSION = "overseas_warranty_in_warranty_v1"
OVERSEAS_WARRANTY_OUT_VERSION = "overseas_warranty_out_of_warranty_v1"
OVERSEAS_WARRANTY_ST_VERSION = "overseas_warranty_st_pickup_v1"
OVERSEAS_SUBJECT = "RMA Authorization: {{ ticket_no }} {{ customer_name }}"
OVERSEAS_COMMON_NOTES = """Please note:
1. Please attach the fault data to the email.
2. Before shipment, please provide photos of the physical goods and outer packaging by email. The nameplate information must be clear for import customs clearance.
3. On your shipping invoice, please state \"No commercial value as sample\".
4. Invoices and packing lists should avoid the following words: old, repaired, returned, used, and national.
5. The recommended declared value is between USD 50 and USD 100.
6. If DHL is used and the value of the goods is less than CNY 5,000, please state \"NO KJ3\" in the commodity name.
7. Please pack the boards separately. Place one or two boards in each box.

Thank you for your cooperation!"""
OVERSEAS_SHIPPING = """Please ship the faulty board to:
Beijing Huafeng Test & Control Technology Co., Ltd.
Attention: Li Lian Rong
Address: Building 5, IC PARK, No. 9 Fenghao East Road, Haidian District (100094), Beijing
Phone: +86-15811322137"""
OVERSEAS_IN_BODY = f"""Dear Customer,

The RMA authorization form is attached for your review. Please print it and include it in the package sent to AccoTEST.
Please ensure that the board is securely packed and that the return address on the RMA form is correct.

{OVERSEAS_SHIPPING}

{OVERSEAS_COMMON_NOTES}"""
OVERSEAS_OUT_BODY = f"""Dear Customer,

The board is out of warranty.
The RMA authorization form is attached for your review. Please print it and include it in the package sent to AccoTEST.
Please ensure that the board is securely packed and that the return address on the RMA form is correct.

{OVERSEAS_SHIPPING}

{OVERSEAS_COMMON_NOTES}"""
OVERSEAS_ST_BODY = """Dear Customer,

The RMA authorization form is attached for your review. Please print it and include it in the package sent to AccoTEST.
Please ensure that the board is securely packed and that the return address on the RMA form is correct.

The following information is required to arrange reverse pick-up:
1. The specific pick-up date and time window. After confirmation, we will arrange for SF Express to collect the package.
2. The detailed pick-up address, contact name, and contact phone number.
3. Package details: total number of boxes, gross weight of each box with units, number of boards in each box, and the SN of every board.
4. Please print the RMA authorization form and place it inside the package.

Before shipment, please provide photos of the physical goods and outer packaging by email. The nameplate information must be clear for import customs clearance.

Thank you for your cooperation!"""


class RmaReplyRuleError(ValueError):
    def __init__(self, task_type: str, reason: str) -> None:
        super().__init__(reason)
        self.task_type = task_type
        self.reason = reason


def _parse_template_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _rma_reply_content(ticket: RepairTicket) -> tuple[str, str, str]:
    if ticket.language_code != "en-US":
        return RMA_ZH_SUBJECT, RMA_ZH_BODY, RMA_REPLY_ZH_VERSION

    email = (ticket.contact_email or "").strip().lower()
    customer = " ".join((ticket.customer_name or "").lower().split())
    if email.endswith("@amkor.com"):
        raise RmaReplyRuleError("rma_amkor_manual", "RMA_AMKOR_MANUAL_HANDLING_REQUIRED")
    if "stmicroelectronics pte ltd" in customer:
        if ticket.request_date and ticket.request_date <= date(2026, 12, 31):
            return OVERSEAS_SUBJECT, OVERSEAS_ST_BODY, OVERSEAS_WARRANTY_ST_VERSION
        raise RmaReplyRuleError("st_policy_expired", "RMA_ST_POLICY_EXPIRED")

    checks = list((ticket.sn_validation_snapshot or {}).get("checks") or [])
    if len(checks) != 1:
        raise RmaReplyRuleError("warranty_status_unknown", "RMA_WARRANTY_EVIDENCE_MISSING")
    warranty_start = _parse_template_date(checks[0].get("warranty_start_date"))
    warranty_end = _parse_template_date(checks[0].get("warranty_end_date"))
    request_date = ticket.request_date
    if not request_date or not warranty_start or not warranty_end or warranty_start > warranty_end or request_date < warranty_start:
        raise RmaReplyRuleError("warranty_status_unknown", "RMA_WARRANTY_STATUS_UNKNOWN")
    if request_date <= warranty_end:
        return OVERSEAS_SUBJECT, OVERSEAS_IN_BODY, OVERSEAS_WARRANTY_IN_VERSION
    if email == "daniel@leitik.com":
        raise RmaReplyRuleError("rma_price_required", "RMA_OUT_OF_WARRANTY_PRICE_REQUIRED")
    return OVERSEAS_SUBJECT, OVERSEAS_OUT_BODY, OVERSEAS_WARRANTY_OUT_VERSION


def serialize_reply(reply: ReplyRecord) -> dict[str, Any]:
    payload = model_to_dict(reply, REPLY_FIELDS)
    payload["rma_template_version"] = normalize_rma_template_version(payload.get("rma_template_version"))
    return payload


def _missing_fields_text(missing_fields: dict[str, Any] | None) -> str:
    if not missing_fields:
        return "- 报修设备 SN\n- 故障描述\n- 联系方式"
    return "\n".join(f"- {key}: {value}" for key, value in missing_fields.items())


def _render_template(template: str, *, ticket: RepairTicket, missing_fields: dict[str, Any] | None) -> str:
    return (
        template.replace("{{ ticket_no }}", ticket.ticket_no)
        .replace("{{ missing_fields }}", _missing_fields_text(missing_fields))
        .replace("{{ customer_name }}", ticket.customer_name or "")
        .replace("{{ contact_person }}", ticket.contact_person or "Customer")
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


def _recipient_addresses(*values: str | None) -> set[str]:
    return {
        address.strip().lower()
        for _, address in getaddresses([value for value in values if value])
        if address.strip()
    }


def _valid_recipient(*values: str | None) -> bool:
    addresses = _recipient_addresses(*values)
    return bool(addresses) and all("@" in address and "." in address.rsplit("@", 1)[-1] for address in addresses)


def _smtp_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD)


def _smtp_sender_is_exact_login() -> bool:
    login = settings.SMTP_USER.strip().lower()
    return bool(login and parseaddr(settings.SMTP_USER)[1].lower() == login == TEST_MAIL_SENDER)


def _rma_envelope_valid(reply: ReplyRecord) -> bool:
    return test_envelope_allowed(reply.to_addresses, reply.cc_addresses)


def _smtp_client() -> smtplib.SMTP:
    if settings.SMTP_PORT == 465:
        return smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20)
    return smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20)


def _recipient_in_whitelist(*values: str | None) -> bool:
    to_addresses = values[0] if values else None
    cc_addresses = values[1] if len(values) > 1 else None
    return test_envelope_allowed(to_addresses, cc_addresses)


def _reply_can_auto_send(reply: ReplyRecord, *, confidence_score: float | None = None, risk_level: str | None = None) -> bool:
    if reply.reply_type == "sn_invalid":
        return False
    enabled = settings.AUTO_FOLLOWUP_ENABLED if is_followup_reply_type(reply.reply_type) else settings.AUTO_SEND_ENABLED
    if not enabled:
        return False
    if not _valid_recipient(reply.to_addresses, reply.cc_addresses):
        return False
    if confidence_score is not None and confidence_score < settings.AUTO_SEND_MIN_CONFIDENCE:
        return False
    if risk_level and risk_level not in {"low", "normal"}:
        return False
    if not _recipient_in_whitelist(reply.to_addresses, reply.cc_addresses):
        return False
    return True


def _smtp_message_id(reply: ReplyRecord) -> str:
    existing = getattr(reply, "smtp_message_id", None)
    if existing:
        return existing
    domain = parseaddr(settings.SMTP_USER)[1].split("@")[-1] if "@" in settings.SMTP_USER else "repair.local"
    reply_id = getattr(reply, "id", None)
    return f"<repair-reply-{reply_id}@{domain}>" if reply_id else make_msgid(domain=domain)


def _build_reply_message(
    reply: ReplyRecord,
    message_id: str,
    *,
    attachment_content: bytes | None = None,
    attachment_filename: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.SMTP_USER
    message["To"] = reply.to_addresses
    if reply.cc_addresses:
        message["Cc"] = reply.cc_addresses
    message["Subject"] = test_only_subject(reply.subject)
    message["Message-ID"] = message_id
    if reply.in_reply_to:
        message["In-Reply-To"] = reply.in_reply_to
    if reply.references_header:
        message["References"] = reply.references_header
    message.set_content(reply.final_body or reply.draft_body or "")
    if attachment_content is not None:
        message.add_attachment(
            attachment_content,
            maintype="application",
            subtype="pdf",
            filename=attachment_filename or "rma-authorization.pdf",
        )
    return message


def _send_reply_via_smtp(
    reply: ReplyRecord,
    *,
    attachment_content: bytes | None = None,
    attachment_filename: str | None = None,
) -> tuple[bool, str | None, str | None]:
    if not _smtp_sender_is_exact_login():
        return False, None, "SMTP_SENDER_LOGIN_MISMATCH"
    if not _rma_envelope_valid(reply):
        return False, None, "SMTP_TEST_ENVELOPE_INVALID"
    if not _recipient_in_whitelist(reply.to_addresses, reply.cc_addresses):
        return False, None, "SMTP_RECIPIENT_NOT_ALLOWED"
    if not _valid_recipient(reply.to_addresses, reply.cc_addresses):
        return False, None, "SMTP_RECIPIENT_INVALID"
    if not _smtp_configured():
        return False, None, "SMTP_NOT_CONFIGURED"
    if settings.SMTP_PORT not in {465, 587}:
        return False, None, "SMTP_TLS_REQUIRED"

    message_id = _smtp_message_id(reply)
    message = _build_reply_message(
        reply,
        message_id,
        attachment_content=attachment_content,
        attachment_filename=attachment_filename,
    )
    try:
        with _smtp_client() as smtp:
            if settings.SMTP_PORT == 587:
                smtp.starttls()
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            # Recheck at the last possible point before the network send.
            if not _recipient_in_whitelist(reply.to_addresses, reply.cc_addresses):
                return False, None, "SMTP_RECIPIENT_NOT_ALLOWED"
            if not _smtp_sender_is_exact_login() or not _rma_envelope_valid(reply):
                return False, None, "SMTP_ENVELOPE_RECHECK_FAILED"
            smtp.send_message(message)
    except Exception:
        return False, None, "SMTP_SEND_FAILED_UNCERTAIN"
    return True, message_id, None


async def _archive_outbound_email(
    session: AsyncSession,
    *,
    reply: ReplyRecord,
    ticket: RepairTicket,
    smtp_message_id: str,
    raw_eml_oss_object_id: int,
    raw_eml_sha256: str,
    attachment_oss_object_id: int | None = None,
    attachment_content: bytes | None = None,
    attachment_filename: str | None = None,
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
        raw_eml_oss_object_id=raw_eml_oss_object_id,
        processing_trace_id=get_correlation_id(),
        source_content_sha256=raw_eml_sha256,
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
    if attachment_oss_object_id and attachment_content is not None:
        existing_attachment = await session.scalar(
            select(EmailAttachment).where(
                EmailAttachment.email_id == email.id,
                EmailAttachment.oss_object_id == attachment_oss_object_id,
            )
        )
        if existing_attachment is None:
            session.add(
                EmailAttachment(
                    email_id=email.id,
                    oss_object_id=attachment_oss_object_id,
                    file_name=attachment_filename or "rma-authorization.pdf",
                    content_type="application/pdf",
                    file_size=len(attachment_content),
                    file_hash=hashlib.sha256(attachment_content).hexdigest(),
                    is_inline=False,
                    parse_status="generated",
                )
            )
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


async def _commit_if_available(session: AsyncSession) -> None:
    commit = getattr(session, "commit", None)
    if commit is not None:
        await commit()


async def _ensure_reply_manual_task(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    task_type: str,
    reason: str,
    email_id: int | None,
    user_id: int | None,
) -> None:
    # Delivery failures for an otherwise export-ready ticket must not destroy
    # the prerequisite needed by a later send reconciliation. The open manual
    # task is the actionable signal while the ticket remains unclosed.
    if ticket.current_status_code == "ready_for_export":
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type=task_type,
            trigger_reason=reason,
            priority="high",
            email_id=email_id,
            assigned_user_id=ticket.assigned_user_id,
        )
        return
    if ticket.current_status_code != "manual_review":
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="manual_review",
            trigger_event="manual_review_required",
            user_id=user_id,
            operator_type="system",
            reason=reason,
            manual_task_type=task_type,
            manual_task_priority="high",
        )
        return
    await create_manual_task_if_missing(
        session,
        ticket=ticket,
        task_type=task_type,
        trigger_reason=reason,
        priority="high",
        email_id=email_id,
        assigned_user_id=ticket.assigned_user_id,
    )


async def _send_reply_record(
    session: AsyncSession,
    *,
    reply: ReplyRecord,
    user_id: int | None,
    auto: bool,
) -> None:
    ticket = await session.get(RepairTicket, reply.ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")
    reply.subject = test_only_subject(reply.subject)

    def sync_ticket_delivery_status() -> None:
        if reply.reply_type == "device_received_ack":
            ticket.device_receipt_ack_status = reply.send_status
        elif reply.reply_type == "rma_authorization" and reply.send_status in {"send_failed", "send_uncertain"}:
            ticket.rma_status = "manual_review"

    if reply.send_status == "sent" and reply.smtp_message_id:
        return
    if reply.send_status in {"sending", "auto_sending", "send_uncertain"}:
        reply.send_status = "send_uncertain"
        reply.error_message = "SMTP_SEND_RESULT_UNCERTAIN"
        sync_ticket_delivery_status()
        await _ensure_reply_manual_task(
            session, ticket=ticket, task_type="reply_send_uncertain",
            reason="SMTP_SEND_RESULT_UNCERTAIN", email_id=reply.related_email_id, user_id=user_id,
        )
        return
    if not _smtp_sender_is_exact_login():
        reply.send_status = "send_failed"
        reply.error_message = "SMTP_SENDER_LOGIN_MISMATCH"
        sync_ticket_delivery_status()
        await _ensure_reply_manual_task(
            session, ticket=ticket, task_type="reply_send_failed",
            reason=reply.error_message, email_id=reply.related_email_id, user_id=user_id,
        )
        return
    if not _rma_envelope_valid(reply):
        reply.send_status = "send_failed"
        reply.error_message = "SMTP_TEST_ENVELOPE_INVALID"
        sync_ticket_delivery_status()
        await _ensure_reply_manual_task(
            session, ticket=ticket, task_type="reply_send_failed",
            reason=reply.error_message, email_id=reply.related_email_id, user_id=user_id,
        )
        return
    if not _recipient_in_whitelist(reply.to_addresses, reply.cc_addresses):
        reply.send_status = "send_failed"
        reply.error_message = "SMTP_RECIPIENT_NOT_ALLOWED"
        sync_ticket_delivery_status()
        await _ensure_reply_manual_task(
            session, ticket=ticket, task_type="reply_send_failed",
            reason="SMTP_RECIPIENT_NOT_ALLOWED", email_id=reply.related_email_id, user_id=user_id,
        )
        return

    attachment_content: bytes | None = None
    attachment_filename: str | None = None
    if reply.rma_pdf_oss_object_id:
        attachment_content = await download_oss_object_bytes(session, oss_object_id=reply.rma_pdf_oss_object_id)
        oss_object = await session.get(OssObject, reply.rma_pdf_oss_object_id)
        attachment_filename = (oss_object.original_file_name if oss_object else None) or f"RMA-{ticket.ticket_no}.pdf"
    message_id = _smtp_message_id(reply)
    reply.smtp_message_id = message_id
    raw_message = _build_reply_message(
        reply,
        message_id,
        attachment_content=attachment_content,
        attachment_filename=attachment_filename,
    ).as_bytes()
    raw_hash = hashlib.sha256(raw_message).hexdigest()
    try:
        raw_object = await upload_bytes_to_oss(
            session,
            content=raw_message,
            original_file_name=f"reply-{reply.id}.eml",
            content_type="message/rfc822",
            source_type="outbound_raw_eml",
            user_id=user_id,
        )
    except (StorageConfigurationError, StorageUploadError) as exc:
        reply.send_status = "send_failed"
        reply.error_message = "OUTBOUND_ARCHIVAL_FAILED"
        sync_ticket_delivery_status()
        await log_system_event(
            session,
            event_type="smtp_send",
            module_name="replies",
            event_stage="outbound_archive",
            event_status="failed",
            target_type="reply_record",
            target_id=reply.id,
            email_id=reply.related_email_id,
            ticket_id=reply.ticket_id,
            severity="error",
            error_code="OUTBOUND_ARCHIVAL_FAILED",
            message="Outbound RFC822 archival failed; SMTP was not called",
            details={"exception_type": exc.__class__.__name__},
        )
        await _ensure_reply_manual_task(
            session, ticket=ticket, task_type="reply_send_failed",
            reason="OUTBOUND_ARCHIVAL_FAILED", email_id=reply.related_email_id, user_id=user_id,
        )
        return

    reply.send_status = "auto_sending" if auto else "sending"
    await _commit_if_available(session)
    async with _smtp_semaphore:
        ok, sent_message_id, error = await asyncio.to_thread(
            _send_reply_via_smtp,
            reply,
            attachment_content=attachment_content,
            attachment_filename=attachment_filename,
        )
    if ok and sent_message_id:
        was_counted = reply.send_status == "sent"
        reply.send_status = "sent"
        reply.sent_at = utcnow()
        reply.error_message = None
        await _archive_outbound_email(
            session,
            reply=reply,
            ticket=ticket,
            smtp_message_id=sent_message_id,
            raw_eml_oss_object_id=raw_object.id,
            raw_eml_sha256=raw_hash,
            attachment_oss_object_id=reply.rma_pdf_oss_object_id,
            attachment_content=attachment_content,
            attachment_filename=attachment_filename,
        )
        if reply.reply_type == "rma_authorization":
            ticket.rma_status = "sent"
            if ticket.device_received_at is not None and ticket.device_receipt_ack_status == "pending_prerequisite":
                from app.services.device_receipts import confirm_device_received

                await confirm_device_received(
                    session,
                    ticket_id=ticket.id,
                    user_id=user_id,
                    source=ticket.device_received_source or "deferred",
                    source_email_id=ticket.device_received_email_id,
                    note=ticket.device_received_note,
                    idempotency_key=ticket.device_received_idempotency_key or f"deferred-device-receipt:{ticket.id}",
                )
        if reply.reply_type == "device_received_ack":
            ticket.device_receipt_ack_status = "sent"
            if ticket.current_status_code == "ready_for_export" and ticket.rma_status == "sent":
                await transition_ticket(
                    session,
                    ticket=ticket,
                    to_status_code="closed",
                    trigger_event="device_receipt_ack_sent",
                    user_id=user_id,
                    operator_type="system" if auto else "user",
                    reason="公司收到待修设备并成功向客户发送收货确认，工单闭合。",
                    metadata={"reply_id": reply.id, "smtp_message_id": sent_message_id},
                )
            else:
                ticket.device_receipt_ack_status = "pending_prerequisite"
                await _ensure_reply_manual_task(
                    session,
                    ticket=ticket,
                    task_type="device_received_prerequisite",
                    reason="DEVICE_RECEIPT_CLOSE_PREREQUISITE_FAILED",
                    email_id=reply.related_email_id,
                    user_id=user_id,
                )
        if is_followup_reply_type(reply.reply_type) and not was_counted:
            ticket.followup_count = min(ticket.max_followup_count, ticket.followup_count + 1)
        if is_followup_reply_type(reply.reply_type) and ticket.current_status_code in {"need_customer_info", "manual_review"}:
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="auto_replied",
                trigger_event="reply_sent",
                user_id=user_id,
                operator_type="system" if auto else "user",
                reason="追问邮件已成功发送。",
                metadata={"reply_id": reply.id, "smtp_message_id": sent_message_id},
            )
    else:
        reply.send_status = "send_uncertain" if error == "SMTP_SEND_FAILED_UNCERTAIN" else "send_failed"
        reply.error_message = error or "SMTP_SEND_FAILED"
        if reply.reply_type == "device_received_ack":
            ticket.device_receipt_ack_status = reply.send_status
        await _ensure_reply_manual_task(
            session,
            ticket=ticket,
            task_type="reply_send_uncertain" if reply.send_status == "send_uncertain" else "reply_send_failed",
            reason=reply.error_message,
            email_id=reply.related_email_id,
            user_id=user_id,
        )
    await log_system_event(
        session,
        event_type="smtp_send",
        module_name="replies",
        event_stage="smtp_send",
        event_status="success" if ok else reply.send_status,
        target_type="reply_record",
        target_id=reply.id,
        email_id=reply.related_email_id,
        ticket_id=reply.ticket_id,
        severity="info" if ok else "error",
        error_code=None if ok else reply.error_message,
        message="SMTP send completed" if ok else "SMTP send requires manual confirmation",
        details={"auto": auto, "outbound_oss_object_id": raw_object.id},
    )
    await log_operation(
        session,
        user_id=user_id,
        operation_type="reply_sent" if ok else "reply_send_failed",
        target_type="reply_record",
        target_id=reply.id,
        email_id=reply.related_email_id,
        ticket_id=reply.ticket_id,
        after_data={"send_status": reply.send_status, "auto": auto},
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
    effective_related_email_id = related_email_id or ticket.source_email_id
    existing_draft = await session.scalar(
        select(ReplyRecord)
        .where(
            ReplyRecord.ticket_id == ticket.id,
            ReplyRecord.related_email_id == effective_related_email_id,
            ReplyRecord.reply_type == reply_kind,
            ReplyRecord.review_status == "pending",
            ReplyRecord.send_status == "pending_review",
        )
        .order_by(ReplyRecord.created_at.desc(), ReplyRecord.id.desc())
    )
    if existing_draft is not None:
        return serialize_reply(existing_draft)
    if is_followup_reply_type(reply_kind) and ticket.followup_count >= ticket.max_followup_count:
        if ticket.current_status_code != "manual_review":
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="manual_review",
                trigger_event="manual_review_required",
                user_id=user_id,
                reason="追问次数已达到上限。",
                manual_task_type="followup_limit",
                manual_task_priority="high",
            )
        else:
            await create_manual_task_if_missing(
                session,
                ticket=ticket,
                task_type="followup_limit",
                trigger_reason="追问次数已达到上限。",
                priority="high",
            )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="FOLLOWUP_LIMIT_EXCEEDED")

    template = await _select_template(session, reply_kind, language)

    related_email = await session.get(Email, effective_related_email_id) if effective_related_email_id else None
    effective_missing_fields = missing_fields if missing_fields is not None else ticket.missing_fields
    if template is None:
        fallback_subject, fallback_body = _fallback_reply_content(reply_kind, ticket=ticket, missing_fields=effective_missing_fields)
        template = SimpleNamespace(id=None, subject_template=fallback_subject, body_template=fallback_body)
        await log_system_event(
            session,
            event_type="reply_template_fallback",
            module_name="replies",
            correlation_id=get_correlation_id(),
            email_id=related_email.id if related_email else None,
            ticket_id=ticket.id,
            event_stage="reply_draft",
            event_status="fallback",
            target_type="ticket",
            target_id=ticket.id,
            message="Reply draft used built-in fallback template",
            details={"reply_type": reply_kind, "language": language},
        )
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
        followup_round=(ticket.followup_count + 1) if is_followup_reply_type(reply_kind) else ticket.followup_count,
        missing_fields=effective_missing_fields,
        to_addresses=TEST_MAIL_RECIPIENT,
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
    if not _recipient_in_whitelist(reply.to_addresses, reply.cc_addresses):
        reply.review_status = "pending"
        logger.warning("Reply draft for ticket_id=%s has non-whitelisted recipient; forcing human review", ticket.id)
    can_auto_send = _reply_can_auto_send(reply, confidence_score=reply_confidence_score, risk_level=reply_risk_level)
    if not can_auto_send:
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="reply_review",
            trigger_reason="回复草稿需要人工确认后发送。",
            email_id=related_email.id if related_email else None,
        )
    if can_auto_send:
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
    if reply.send_status == "sent":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="REPLY_ALREADY_SENT_IMMUTABLE")
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
    if reply.send_status == "sent":
        return {"reply": serialize_reply(reply), "auto_send_enabled": settings.AUTO_SEND_ENABLED, "reply_send_mode": settings.REPLY_SEND_MODE}
    if reply.send_status in {"sending", "auto_sending", "send_uncertain"}:
        await _send_reply_record(session, reply=reply, user_id=user_id, auto=False)
        return {"reply": serialize_reply(reply), "auto_send_enabled": settings.AUTO_SEND_ENABLED, "reply_send_mode": settings.REPLY_SEND_MODE}
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
        email_id=reply.related_email_id,
        ticket_id=reply.ticket_id,
        after_data={
            "send_status": reply.send_status,
            "auto_send_enabled": settings.AUTO_SEND_ENABLED,
            "reply_send_mode": settings.REPLY_SEND_MODE,
        },
    )
    return {"reply": serialize_reply(reply), "auto_send_enabled": settings.AUTO_SEND_ENABLED, "reply_send_mode": settings.REPLY_SEND_MODE}


async def approve_reply_for_async(session: AsyncSession, *, reply_id: int, user_id: int) -> ReplyRecord:
    reply = await get_reply(session, reply_id)
    if reply.send_status == "sent":
        return reply
    if reply.send_status in {"sending", "auto_sending", "send_uncertain"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="REPLY_SEND_RESULT_UNCERTAIN")
    reply.review_status = "approved"
    reply.reviewed_by_user_id = user_id
    reply.reviewed_at = utcnow()
    reply.send_status = "approved_pending_send"
    reply.error_message = None
    await log_operation(
        session,
        user_id=user_id,
        operation_type="reply_approved_for_async_send",
        target_type="reply_record",
        target_id=reply.id,
        email_id=reply.related_email_id,
        ticket_id=reply.ticket_id,
        after_data={"send_status": reply.send_status},
    )
    return reply


async def reject_reply(session: AsyncSession, *, reply_id: int, user_id: int, reason: str) -> dict[str, Any]:
    reply = await get_reply(session, reply_id)
    if reply.send_status == "sent":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="REPLY_ALREADY_SENT_IMMUTABLE")
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


async def reconcile_uncertain_reply(
    session: AsyncSession,
    *,
    reply_id: int,
    user_id: int,
    outcome: str,
    reason: str,
    smtp_message_id: str | None = None,
) -> dict[str, Any]:
    reply = await session.get(ReplyRecord, reply_id, with_for_update=True)
    if reply is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="REPLY_NOT_FOUND")
    if reply.send_status != "send_uncertain":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="REPLY_SEND_NOT_UNCERTAIN")
    ticket = await session.get(RepairTicket, reply.ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")

    if outcome == "sent":
        reply.send_status = "sent"
        reply.sent_at = utcnow()
        reply.smtp_message_id = smtp_message_id or reply.smtp_message_id
        reply.error_message = None
        if is_followup_reply_type(reply.reply_type):
            ticket.followup_count = min(ticket.max_followup_count, ticket.followup_count + 1)
            if ticket.current_status_code in {"need_customer_info", "manual_review"}:
                await transition_ticket(
                    session,
                    ticket=ticket,
                    to_status_code="auto_replied",
                    trigger_event="reply_sent",
                    user_id=user_id,
                    operator_type="user",
                    reason="人工确认结果不确定的追问邮件实际已成功发送。",
                    metadata={"reply_id": reply.id, "reconciled": True},
                )
        elif reply.reply_type == "rma_authorization":
            ticket.rma_status = "sent"
        elif reply.reply_type == "device_received_ack":
            ticket.device_receipt_ack_status = "sent"
            if ticket.current_status_code == "ready_for_export" and ticket.rma_status == "sent":
                await transition_ticket(
                    session,
                    ticket=ticket,
                    to_status_code="closed",
                    trigger_event="device_receipt_ack_sent",
                    user_id=user_id,
                    operator_type="user",
                    reason="人工确认收货回复实际已发送，工单闭合。",
                    metadata={"reply_id": reply.id, "reconciled": True},
                )
    elif outcome == "failed":
        reply.send_status = "send_failed"
        reply.error_message = "SMTP_SEND_CONFIRMED_FAILED"
        if reply.reply_type == "device_received_ack":
            ticket.device_receipt_ack_status = "send_failed"
        if reply.reply_type == "rma_authorization":
            ticket.rma_status = "manual_review"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="REPLY_RECONCILE_OUTCOME_INVALID")

    await log_operation(
        session,
        user_id=user_id,
        operation_type="reply_send_reconciled",
        target_type="reply_record",
        target_id=reply.id,
        ticket_id=ticket.id,
        description=reason,
        after_data={"outcome": outcome, "send_status": reply.send_status},
    )
    return serialize_reply(reply)


async def _rma_manual_review(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    task_type: str,
    reason: str,
) -> dict[str, Any]:
    ticket.rma_status = "manual_review"
    await create_manual_task_if_missing(
        session,
        ticket=ticket,
        task_type=task_type,
        trigger_reason=reason,
        priority="high",
        assigned_user_id=ticket.assigned_user_id,
    )
    return {"status": "manual_review", "error_code": reason, "ticket_id": ticket.id}


async def create_and_send_rma_authorization(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_id: int | None,
    expected_version: int,
    expected_safety_hash: str,
    expected_sn_validation_hash: str,
    expected_rma_template_version: str,
) -> dict[str, Any]:
    ticket = await session.get(RepairTicket, ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")
    if not ticket.rma_required:
        ticket.rma_status = "not_required"
        return {"status": "not_required", "ticket_id": ticket.id}
    if ticket.current_status_code != "ready_for_export":
        return await _rma_manual_review(session, ticket=ticket, task_type="rma_state_invalid", reason="RMA_TICKET_NOT_READY")
    if (
        ticket.version != expected_version
        or ticket.safety_check_hash != expected_safety_hash
        or ticket.sn_validation_hash != expected_sn_validation_hash
        or (ticket.safety_check_snapshot or {}).get("sn_validation_hash") != expected_sn_validation_hash
        or normalize_rma_template_version(expected_rma_template_version) != RMA_TEMPLATE_VERSION
    ):
        return {"status": "superseded", "error_code": "TICKET_SNAPSHOT_SUPERSEDED", "ticket_id": ticket.id}
    attach_rma = bool(settings.RMA_AUTO_SEND_ENABLED)
    reply_type = "rma_authorization" if attach_rma else "receipt"
    existing = await session.scalar(
        select(ReplyRecord)
        .where(
            ReplyRecord.ticket_id == ticket.id,
            ReplyRecord.reply_type == reply_type,
            ReplyRecord.send_status.in_({"pending_review", "approved_pending_send", "sending", "auto_sending", "sent", "send_uncertain"}),
        )
        .order_by(ReplyRecord.id.desc())
    )
    if existing is not None:
        if existing.send_status == "sent" and attach_rma:
            ticket.rma_status = "sent"
        return {"status": existing.send_status, "ticket_id": ticket.id, "reply_id": existing.id, "idempotent_reuse": True}

    pdf_content: bytes | None = None
    pdf_object: OssObject | None = None
    data = None
    if attach_rma:
        try:
            subject_template, body_template, reply_template_version = _rma_reply_content(ticket)
        except RmaReplyRuleError as exc:
            return await _rma_manual_review(session, ticket=ticket, task_type=exc.task_type, reason=exc.reason)

        ticket.rma_status = "generating"
        try:
            data = await build_rma_pdf_data(
                session,
                ticket_id=ticket.id,
                safety_snapshot=ticket.safety_check_snapshot,
            )
            pdf_content = await asyncio.to_thread(render_rma_pdf, data, test_only=True)
            file_name = rma_pdf_file_name(data)
            pdf_object = await upload_bytes_to_oss(
                session,
                content=pdf_content,
                original_file_name=file_name,
                content_type="application/pdf",
                source_type="rma_authorization_pdf",
                user_id=user_id,
            )
        except (RmaPdfError, StorageConfigurationError, StorageUploadError) as exc:
            return await _rma_manual_review(session, ticket=ticket, task_type="rma_generation_failed", reason=str(exc)[:100])
    else:
        subject_template = "报修申请已受理：{{ ticket_no }} {{ customer_name }}"
        body_template = (
            "您好，{{ contact_person }}：\n\n"
            "我们已收到并确认您的报修申请。RMA 维修授权单附件当前未启用自动发送，"
            "后续将由工作人员单独处理。\n\n谢谢。"
        )
        reply_template_version = "new_repair_receipt_without_rma_v1"

    related_email = await session.get(Email, ticket.source_email_id) if ticket.source_email_id else None
    subject = _render_template(subject_template, ticket=ticket, missing_fields=None)
    body = _render_template(body_template, ticket=ticket, missing_fields=None)
    reply = ReplyRecord(
        ticket_id=ticket.id,
        related_email_id=related_email.id if related_email else None,
        reply_type=reply_type,
        followup_round=ticket.followup_count,
        to_addresses=TEST_MAIL_RECIPIENT,
        cc_addresses=None,
        subject=subject,
        draft_body=body,
        final_body=body,
        generate_source="rma_template" if attach_rma else "new_repair_receipt",
        rma_pdf_oss_object_id=pdf_object.id if pdf_object else None,
        reply_template_version=reply_template_version,
        rma_template_version=RMA_TEMPLATE_VERSION if attach_rma else None,
        rma_pdf_data_snapshot=(
            rma_pdf_snapshot(data, pdf_content=pdf_content, oss_object_id=pdf_object.id)
            if data is not None and pdf_content is not None and pdf_object is not None
            else None
        ),
        review_status="auto_approved" if settings.AUTO_SEND_ENABLED else "pending",
        reviewed_at=utcnow() if settings.AUTO_SEND_ENABLED else None,
        send_status="approved_pending_send" if settings.AUTO_SEND_ENABLED else "pending_review",
        in_reply_to=related_email.message_id if related_email else None,
        references_header=related_email.references_header if related_email else None,
    )
    session.add(reply)
    await session.flush()
    if not settings.AUTO_SEND_ENABLED:
        ticket.rma_status = "manual_review"
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="rma_reply_review" if attach_rma else "rma_attachment_disabled",
            trigger_reason="普通回复自动发送已关闭，需要人工审核新报修回复。",
            priority="high" if attach_rma else "normal",
            email_id=related_email.id if related_email else None,
        )
        return {"status": "pending_review", "ticket_id": ticket.id, "reply_id": reply.id, "idempotent_reuse": False}

    ticket.rma_status = "sending" if attach_rma else "manual_review"
    await _send_reply_record(session, reply=reply, user_id=user_id, auto=True)
    if reply.send_status == "sent":
        if not attach_rma:
            ticket.rma_status = "manual_review"
            await create_manual_task_if_missing(
                session,
                ticket=ticket,
                task_type="rma_attachment_disabled",
                trigger_reason="新报修确认已发送，但 RMA 授权单附件未发送。",
                priority="normal",
                email_id=related_email.id if related_email else None,
            )
        await log_operation(
            session,
            user_id=user_id,
            operation_type="rma_authorization_sent",
            target_type="repair_ticket",
            target_id=ticket.id,
            ticket_id=ticket.id,
            after_data={
                "reply_id": reply.id,
                "pdf_oss_object_id": pdf_object.id if pdf_object else None,
                "recipient": reply.to_addresses,
                "reply_template_version": reply.reply_template_version,
                "rma_template_version": reply.rma_template_version,
            },
        )
        return {
            "status": "succeeded" if attach_rma else "reply_sent_rma_pending",
            "ticket_id": ticket.id,
            "reply_id": reply.id,
            "idempotent_reuse": False,
        }
    ticket.rma_status = "manual_review"
    return {"status": "manual_review", "error_code": reply.error_message or reply.send_status, "ticket_id": ticket.id, "reply_id": reply.id}
