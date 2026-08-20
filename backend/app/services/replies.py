from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import smtplib
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parseaddr
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.request_context import get_correlation_id
from app.models import (
    Email,
    EmailAttachment,
    EmailThread,
    EmailTicketLink,
    ManualReviewTask,
    OssObject,
    ReplyRecord,
    ReplyTemplate,
    RepairTicket,
    RepairTicketItem,
    TicketRma,
)
from app.services.audit import log_operation, log_system_event
from app.services.business_rules import is_followup_reply_type, required_missing_for_ticket
from app.services.business_notifications import notify_ticket_once
from app.services.common import model_to_dict, utcnow
from app.services.external_operations import (
    fail_external_operation,
    get_external_operation,
    start_external_operation,
    succeed_external_operation,
)
from app.services.mail_safety import TEST_MAIL_RECIPIENT, TEST_MAIL_SENDER, test_envelope_allowed, test_only_subject
from app.services.mail_reply_renderer import (
    RelatedResource,
    ReplyRenderError,
    render_reply_history,
)
from app.resources.signature_logo import ACCO_TEST_LOGO_CONTENT_ID, ACCO_TEST_LOGO_PNG
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

RMA_TASK_TYPES_RESOLVED_ON_SEND = frozenset(
    {
        "rma_state_invalid",
        "rma_number_not_unique",
        "rma_branding_policy_conflict",
        "rma_neutral_pdf_template_required",
        "rma_reply_parent_required",
        "rma_reply_template_missing",
        "rma_reply_base_template_missing",
        "rma_generation_failed",
        "rma_reply_review",
        "rma_attachment_disabled",
        "sap_export_policy_or_address_conflict",
        "sap_rma_number_invalid",
        "sap_rma_timeout",
        "duplicate_rma_number",
        "multiple_rma_numbers_for_ticket",
        "warranty_status_unknown",
        "rma_special_policy_review",
        "rma_amkor_manual",
        "rma_st_manual",
        "rma_price_required",
    }
)
ACTIVE_MANUAL_TASK_STATUSES = frozenset(
    {"pending", "assigned", "claimed", "assignment_failed"}
)

REPLY_FIELDS = (
    "id",
    "ticket_id",
    "related_email_id",
    "outgoing_email_id",
    "template_id",
    "base_template_id",
    "reply_type",
    "followup_round",
    "missing_fields",
    "to_addresses",
    "cc_addresses",
    "subject",
    "draft_body",
    "final_body",
    "draft_html_body",
    "final_html_body",
    "thread_history_hash",
    "render_hash",
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
    "archive_status",
    "send_attempt_count",
    "archive_attempt_count",
    "smtp_message_id",
    "smtp_response",
    "thread_version",
    "in_reply_to",
    "references_header",
    "sent_at",
    "archive_verified_at",
    "next_retry_at",
    "last_error_code",
    "error_message",
    "created_at",
    "updated_at",
)


def _message_id_chain(*values: str | None) -> str | None:
    message_ids: list[str] = []
    for value in values:
        if not value:
            continue
        found = re.findall(r"<[^<>\s]+>", value)
        if not found and "@" in value:
            found = [value if value.startswith("<") else f"<{value.strip('<>')}>"]
        for message_id in found:
            if message_id not in message_ids:
                message_ids.append(message_id)
    return " ".join(message_ids[-50:]) or None


async def resolve_completed_rma_tasks(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_id: int | None,
) -> int:
    tasks = list(
        (
            await session.execute(
                select(ManualReviewTask).where(
                    ManualReviewTask.ticket_id == ticket_id,
                    ManualReviewTask.task_type.in_(
                        RMA_TASK_TYPES_RESOLVED_ON_SEND
                    ),
                    ManualReviewTask.status.in_(
                        ACTIVE_MANUAL_TASK_STATUSES
                    ),
                )
            )
        ).scalars().all()
    )
    if not tasks:
        return 0

    from app.services.notifications import resolve_notifications_for_target

    resolved_at = utcnow()
    for task in tasks:
        task.status = "resolved"
        task.resolved_by_user_id = user_id
        task.resolved_at = resolved_at
        task.resolution = (
            "RMA reply was sent successfully; the earlier RMA-stage "
            "exception is no longer active."
        )
        await resolve_notifications_for_target(
            session,
            target_type="manual_review_task",
            target_id=task.id,
        )
    return len(tasks)


def _reply_subject(subject: str | None, ticket_no: str) -> str:
    base = (subject or f"Repair request {ticket_no}").strip()
    base = re.sub(r"^(?:(?:re|fw|fwd)\s*:\s*)+", "", base, flags=re.IGNORECASE).strip()
    return f"Re: {base}"[:500]


async def _latest_customer_email(session: AsyncSession, ticket: RepairTicket) -> Email | None:
    return await session.scalar(
        select(Email)
        .join(EmailTicketLink, EmailTicketLink.email_id == Email.id)
        .where(EmailTicketLink.ticket_id == ticket.id, Email.mail_direction == "inbound")
        .order_by(Email.received_at.desc(), Email.id.desc())
        .limit(1)
    )


async def _current_thread_version(
    session: AsyncSession,
    ticket: RepairTicket,
) -> int | None:
    thread = await session.get(EmailThread, ticket.thread_id) if ticket.thread_id else None
    return thread.thread_version if thread is not None else None


async def _reply_parent_error(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    candidate: Email | None,
) -> str | None:
    if candidate is None:
        return "REPLY_PARENT_EMAIL_REQUIRED"
    if candidate.mail_direction != "inbound":
        return "REPLY_PARENT_MUST_BE_INBOUND"
    if not _message_id_chain(candidate.message_id):
        return "REPLY_PARENT_MESSAGE_ID_REQUIRED"
    if ticket.thread_id is None or candidate.thread_id != ticket.thread_id:
        return "REPLY_PARENT_THREAD_MISMATCH"
    if candidate.id == ticket.source_email_id:
        return None
    linked = await session.scalar(
        select(EmailTicketLink.id).where(
            EmailTicketLink.email_id == candidate.id,
            EmailTicketLink.ticket_id == ticket.id,
        )
    )
    return None if linked is not None else "REPLY_PARENT_NOT_LINKED_TO_TICKET"


async def _require_reply_parent(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    related_email_id: int | None = None,
) -> Email:
    if related_email_id is not None:
        candidate = await session.get(Email, related_email_id)
        error = await _reply_parent_error(session, ticket=ticket, candidate=candidate)
        if error is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error)
        return candidate

    candidates = [
        await _latest_customer_email(session, ticket),
        await session.get(Email, ticket.source_email_id) if ticket.source_email_id else None,
    ]
    errors: list[str] = []
    for candidate in candidates:
        error = await _reply_parent_error(session, ticket=ticket, candidate=candidate)
        if error is None:
            return candidate
        errors.append(error)
    detail = "REPLY_PARENT_EMAIL_REQUIRED" if all(item == "REPLY_PARENT_EMAIL_REQUIRED" for item in errors) else errors[0]
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


RMA_REPLY_ZH_IN_WARRANTY_VERSION = "domestic_in_warranty_v1"
RMA_REPLY_ZH_OUT_OF_WARRANTY_VERSION = "domestic_out_warranty_v1"
OVERSEAS_WARRANTY_IN_VERSION = "overseas_in_warranty_v1"
OVERSEAS_WARRANTY_OUT_VERSION = "overseas_out_warranty_v1"
OVERSEAS_WARRANTY_ST_VERSION = "overseas_st_pickup_v1"


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


def _rma_reply_template_type(ticket: RepairTicket) -> tuple[str, str]:
    email = (ticket.contact_email or "").strip().lower()
    customer = " ".join((ticket.customer_name or "").lower().split())
    if ticket.language_code == "en-US" and email.endswith("@amkor.com"):
        raise RmaReplyRuleError("rma_amkor_manual", "RMA_AMKOR_MANUAL_HANDLING_REQUIRED")
    if ticket.language_code == "en-US" and "stmicroelectronics pte ltd" in customer:
        raise RmaReplyRuleError(
            "rma_st_manual",
            "RMA_ST_CUSTOM_HANDLING_REQUIRES_MANUAL",
        )

    checks = list((ticket.sn_validation_snapshot or {}).get("checks") or [])
    if not checks:
        raise RmaReplyRuleError("warranty_status_unknown", "RMA_WARRANTY_EVIDENCE_MISSING")
    request_date = ticket.request_date
    warranty_flags: set[bool] = set()
    for check in checks:
        warranty_start = _parse_template_date(check.get("warranty_start_date"))
        warranty_end = _parse_template_date(check.get("warranty_end_date"))
        if not request_date or not warranty_start or not warranty_end or warranty_start > warranty_end or request_date < warranty_start:
            raise RmaReplyRuleError("warranty_status_unknown", "RMA_WARRANTY_STATUS_UNKNOWN")
        warranty_flags.add(request_date <= warranty_end)
    if len(warranty_flags) != 1:
        raise RmaReplyRuleError("warranty_status_unknown", "RMA_MIXED_WARRANTY_STATUS")
    in_warranty = True in warranty_flags
    if ticket.language_code != "en-US":
        if in_warranty:
            return "rma_authorization_domestic_in_warranty", RMA_REPLY_ZH_IN_WARRANTY_VERSION
        return "rma_authorization_domestic_out_of_warranty", RMA_REPLY_ZH_OUT_OF_WARRANTY_VERSION
    if in_warranty:
        return "rma_authorization_overseas_in_warranty", OVERSEAS_WARRANTY_IN_VERSION
    if email == "daniel@leitik.com":
        raise RmaReplyRuleError("rma_price_required", "RMA_OUT_OF_WARRANTY_PRICE_REQUIRED")
    return "rma_authorization_overseas_out_of_warranty", OVERSEAS_WARRANTY_OUT_VERSION


def serialize_reply(reply: ReplyRecord) -> dict[str, Any]:
    payload = model_to_dict(reply, REPLY_FIELDS)
    payload["rma_template_version"] = normalize_rma_template_version(payload.get("rma_template_version"))
    return payload


def _missing_fields_text(missing_fields: dict[str, Any] | None) -> str:
    if not missing_fields:
        return "- 报修设备 SN\n- 故障描述\n- 联系方式"
    return "\n".join(f"- {key}: {value}" for key, value in missing_fields.items())


THREAD_HISTORY_SEPARATOR = "\n\n________________________________\n"


def _plain_to_html(value: str) -> str:
    return '<div style="font-family:Arial,Helvetica,sans-serif;white-space:normal">' + html.escape(value).replace("\n", "<br>\n") + "</div>"


def _render_hash(*, subject: str | None, plain: str | None, html_body: str | None, history_hash: str | None) -> str:
    payload = "\x1f".join((subject or "", plain or "", html_body or "", history_hash or ""))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _thread_history(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    language: str,
) -> tuple[str, str, str]:
    rows = list(
        (
            await session.execute(
                select(Email)
                .where(Email.thread_id == ticket.thread_id)
                .order_by(Email.sent_at.asc(), Email.received_at.asc(), Email.id.asc())
            )
        ).scalars().all()
    ) if ticket.thread_id else []
    attachment_rows = list(
        (
            await session.execute(
                select(EmailAttachment).where(EmailAttachment.email_id.in_([row.id for row in rows]))
            )
        ).scalars().all()
    ) if rows else []
    attachments: dict[int, list[str]] = {}
    for attachment in attachment_rows:
        attachments.setdefault(attachment.email_id, []).append(attachment.file_name)

    blocks: list[str] = []
    for row in rows:
        timestamp = row.sent_at or row.received_at or row.created_at
        body = (row.latest_reply_segment or row.clean_body or row.text_body or "").strip()
        names = sorted(set(attachments.get(row.id, [])))
        if language == "zh-CN":
            labels = ("发件人", "发送时间", "收件人", "抄送", "主题", "附件")
        else:
            labels = ("From", "Sent", "To", "Cc", "Subject", "Attachments")
        header = [
            f"{labels[0]}： {row.from_address}",
            f"{labels[1]}： {timestamp.strftime('%Y-%m-%d %H:%M') if timestamp else ''}",
            f"{labels[2]}： {row.to_addresses or ''}",
        ]
        if row.cc_addresses:
            header.append(f"{labels[3]}： {row.cc_addresses}")
        header.append(f"{labels[4]}： {row.subject or ''}")
        if names:
            header.append(f"{labels[5]}： {', '.join(names)}")
        blocks.append("\n".join((*header, body)).strip())
    plain = THREAD_HISTORY_SEPARATOR.join(blocks)
    html_body = "<br><br>".join(_plain_to_html(block) for block in blocks)
    digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    return plain, html_body, digest


def _render_template(
    template: str,
    *,
    ticket: RepairTicket,
    missing_fields: dict[str, Any] | None,
    content: str = "",
    original_subject: str = "",
    return_address_block: str = "",
    city: str = "",
    repair_fee: str = "",
    currency_unit: str = "",
    escape_values: bool = False,
) -> str:
    def value(item: str) -> str:
        return html.escape(item) if escape_values else item

    return (
        template.replace("{{ ticket_no }}", value(ticket.ticket_no))
        .replace("{{ missing_fields }}", value(_missing_fields_text(missing_fields)))
        .replace("{{ customer_name }}", value(ticket.customer_name or ""))
        .replace("{{ contact_person }}", value(ticket.contact_person or "Customer"))
        .replace("{{ content }}", content if escape_values else value(content))
        .replace("{{ original_subject }}", value(original_subject))
        .replace("{{ return_address_block }}", value(return_address_block))
        .replace("{{ city }}", value(city))
        .replace("{{ repair_fee }}", value(repair_fee))
        .replace("{{ currency_unit }}", value(currency_unit))
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
    related_resources: tuple[RelatedResource, ...] = (),
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
    rendered_html = getattr(reply, "final_html_body", None) or getattr(reply, "draft_html_body", None)
    if rendered_html:
        message.add_alternative(rendered_html, subtype="html")
        html_part = message.get_body(preferencelist=("html",))
        if html_part is not None:
            resources = list(related_resources)
            if f"cid:{ACCO_TEST_LOGO_CONTENT_ID}" in rendered_html:
                resources.insert(
                    0,
                    RelatedResource(
                        content=ACCO_TEST_LOGO_PNG,
                        maintype="image",
                        subtype="png",
                        content_id=ACCO_TEST_LOGO_CONTENT_ID,
                        original_content_id=ACCO_TEST_LOGO_CONTENT_ID,
                        content_hash=hashlib.sha256(ACCO_TEST_LOGO_PNG).hexdigest(),
                    ),
                )
            seen_cids: set[str] = set()
            for resource in resources:
                if resource.content_id in seen_cids:
                    continue
                seen_cids.add(resource.content_id)
                html_part.add_related(
                    resource.content,
                    maintype=resource.maintype,
                    subtype=resource.subtype,
                    cid=f"<{resource.content_id}>",
                    disposition="inline",
                )
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
    message: EmailMessage | None = None,
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
    if message is None:
        message = _build_reply_message(
            reply,
            message_id,
            attachment_content=attachment_content,
            attachment_filename=attachment_filename,
        )
    elif str(message.get("Message-ID") or "") != message_id:
        return False, None, "SMTP_MESSAGE_ID_MISMATCH"
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


async def _select_base_template(
    session: AsyncSession,
    language: str,
    *,
    hide_company_name: bool = False,
) -> ReplyTemplate | None:
    if language == "en-US" and not hide_company_name:
        return await _select_template(session, "international_company_base", language)
    if language != "zh-CN":
        return None
    return await _select_template(
        session,
        "neutral_base" if hide_company_name else "domestic_company_base",
        language,
    )


def _return_address_block(
    *,
    language: str,
    customer_policy: dict[str, Any] | None = None,
) -> str:
    policy = customer_policy or {}
    if language == "en-US":
        return settings.RMA_OVERSEAS_BEIJING_ADDRESS_BLOCK
    route = str(policy.get("shipping_route") or policy.get("return_location") or "").strip().lower()
    default_company = {
        "beijing": settings.RMA_DEFAULT_BEIJING_COMPANY,
        "tianjin": settings.RMA_DEFAULT_TIANJIN_COMPANY,
    }.get(route, "")
    default_address = {
        "beijing": settings.RMA_DEFAULT_BEIJING_ADDRESS,
        "tianjin": settings.RMA_DEFAULT_TIANJIN_ADDRESS,
    }.get(route, "")
    company = str(policy.get("shipping_company") or default_company).strip()
    address = str(policy.get("shipping_address") or default_address).strip()
    if company and address.startswith(company):
        address = address[len(company):].lstrip(" \t\r\n　，,；;")
    contact = str(policy.get("shipping_contact") or "").strip()
    phone = str(policy.get("shipping_phone") or "").strip()
    postal_code = str(policy.get("shipping_postal_code") or "").strip()
    contact_separator = "" if postal_code else "  "
    contact_line = (
        f"{contact}{contact_separator}电话：{phone}"
        if contact or phone
        else ""
    )
    if postal_code:
        contact_line = f"{contact_line}；邮编：{postal_code}"
    return "\n".join(value for value in (company, address, contact_line) if value)


async def _render_reply_templates(
    session: AsyncSession,
    *,
    content_template: ReplyTemplate,
    ticket: RepairTicket,
    missing_fields: dict[str, Any] | None,
    parent: Email,
    customer_policy: dict[str, Any] | None = None,
) -> tuple[str, str, str, ReplyTemplate | None, str, str]:
    original_subject = (parent.subject or f"Repair request {ticket.ticket_no}").strip()
    policy = customer_policy or {}
    city = {"beijing": "北京", "tianjin": "天津"}.get(
        str(policy.get("shipping_route") or "").strip().lower(), ""
    )
    repair_fee = str(policy.get("repair_price") or "").strip()
    currency = str(policy.get("currency") or "").strip().upper()
    currency_unit = {"CNY": "RMB", "RMB": "RMB", "USD": "USD"}.get(currency, currency)
    return_address_block = _return_address_block(
        language=content_template.language,
        customer_policy=policy,
    )
    content = _render_template(
        content_template.body_template,
        ticket=ticket,
        missing_fields=missing_fields,
        original_subject=original_subject,
        return_address_block=return_address_block,
        city=city,
        repair_fee=repair_fee,
        currency_unit=currency_unit,
    )
    salutation = str((customer_policy or {}).get("reply_salutation") or "").strip()
    if salutation:
        if content.startswith("您好"):
            content = salutation + content[2:]
        elif content.startswith("Dear Customer"):
            content = salutation + content[len("Dear Customer"):]
    base_template = await _select_base_template(
        session,
        content_template.language,
        hide_company_name=bool((customer_policy or {}).get("hide_company_name")),
    )
    if (
        content_template.language in {"zh-CN", "en-US"}
        and not bool((customer_policy or {}).get("hide_company_name"))
        and base_template is None
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="REPLY_BASE_TEMPLATE_NOT_FOUND")
    body = (
        _render_template(
            base_template.body_template,
            ticket=ticket,
            missing_fields=missing_fields,
            content=content,
            original_subject=original_subject,
            return_address_block=return_address_block,
            city=city,
            repair_fee=repair_fee,
            currency_unit=currency_unit,
        )
        if base_template is not None
        else content
    )
    if content_template.html_body_template:
        html_content = _render_template(
            content_template.html_body_template,
            ticket=ticket,
            missing_fields=missing_fields,
            original_subject=original_subject,
            return_address_block=return_address_block,
            city=city,
            repair_fee=repair_fee,
            currency_unit=currency_unit,
            escape_values=True,
        )
    else:
        html_content = _plain_to_html(content)
    if base_template is not None and base_template.html_body_template:
        html_body = _render_template(
            base_template.html_body_template,
            ticket=ticket,
            missing_fields=missing_fields,
            content=html_content,
            original_subject=original_subject,
            return_address_block=return_address_block,
            city=city,
            repair_fee=repair_fee,
            currency_unit=currency_unit,
            escape_values=True,
        )
    else:
        html_body = html_content

    history = await render_reply_history(
        session,
        parent=parent,
        language=content_template.language,
    )
    body = body.rstrip() + THREAD_HISTORY_SEPARATOR + history.plain
    html_body = html_body.rstrip() + '<hr style="border:0;border-top:1px solid #999">' + history.html
    subject = _reply_subject(parent.subject, ticket.ticket_no)
    return subject, body, html_body, base_template, history.snapshot_hash, _render_hash(
        subject=subject,
        plain=body,
        html_body=html_body,
        history_hash=history.snapshot_hash,
    )


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
    latest_segment = body.split(THREAD_HISTORY_SEPARATOR, 1)[0].rstrip()
    transport_subject = test_only_subject(reply.subject)
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
        # Persist the subject that was actually serialized and accepted by
        # SMTP.  The reply record intentionally keeps the business subject so
        # the RMA subject and PDF basename remain comparable.
        subject=transport_subject,
        normalized_subject=transport_subject,
        sent_at=reply.sent_at,
        received_at=reply.sent_at,
        text_body=body,
        html_body=reply.final_html_body or reply.draft_html_body,
        clean_body=body,
        latest_reply_segment=latest_segment,
        parse_status="sent",
        processing_stage="completed",
        intent_type="outbound_reply",
        terminal_reason_code="OUTBOUND_REPLY_SENT",
        retryable=False,
    )
    session.add(email)
    await session.flush()
    reply.outgoing_email_id = email.id
    thread = await session.get(EmailThread, ticket.thread_id) if ticket.thread_id else None
    if thread is not None:
        thread.latest_email_id = email.id
        thread.email_count = (thread.email_count or 0) + 1
        thread.thread_version = (thread.thread_version or 0) + 1
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


async def _finalize_rma_issue(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    reply: ReplyRecord,
    user_id: int | None,
    auto: bool,
) -> bool:
    if reply.reply_type != "rma_authorization":
        return False
    rma_record = await session.scalar(
        select(TicketRma).where(TicketRma.ticket_id == ticket.id)
    )
    if rma_record is None:
        reply.archive_status = "archive_failed"
        reply.last_error_code = "RMA_RECORD_REQUIRED"
        reply.error_message = "RMA_RECORD_REQUIRED"
        return False
    if (
        reply.send_status != "sent"
        or not reply.smtp_message_id
        or not reply.outgoing_email_id
        or not reply.rma_pdf_oss_object_id
        or rma_record.pdf_oss_object_id != reply.rma_pdf_oss_object_id
    ):
        reply.archive_status = "archive_pending"
        reply.last_error_code = "RMA_ISSUE_ARCHIVE_PREREQUISITE_MISSING"
        return False
    outgoing = await session.get(Email, reply.outgoing_email_id)
    pdf_object = await session.get(OssObject, reply.rma_pdf_oss_object_id)
    attachment = await session.scalar(
        select(EmailAttachment).where(
            EmailAttachment.email_id == reply.outgoing_email_id,
            EmailAttachment.oss_object_id == reply.rma_pdf_oss_object_id,
        )
    )
    snapshot = reply.rma_pdf_data_snapshot or {}
    expected_hash = str(snapshot.get("pdf_sha256") or "")
    if (
        outgoing is None
        or outgoing.raw_eml_oss_object_id is None
        or pdf_object is None
        or attachment is None
        or not expected_hash
        or attachment.file_hash != expected_hash
    ):
        reply.archive_status = "archive_failed"
        reply.archive_attempt_count = int(reply.archive_attempt_count or 0) + 1
        reply.last_error_code = "RMA_ISSUE_ARCHIVE_VERIFICATION_FAILED"
        reply.error_message = "RMA_ISSUE_ARCHIVE_VERIFICATION_FAILED"
        rma_record.pdf_archive_status = "failed"
        await _ensure_reply_manual_task(
            session,
            ticket=ticket,
            task_type="rma_archive_failed",
            reason="RMA_ISSUE_ARCHIVE_VERIFICATION_FAILED",
            email_id=reply.related_email_id,
            user_id=user_id,
        )
        return False

    now = utcnow()
    reply.archive_status = "archived"
    reply.archive_attempt_count = int(reply.archive_attempt_count or 0) + 1
    reply.archive_verified_at = now
    reply.next_retry_at = None
    reply.last_error_code = None
    reply.error_message = None
    rma_record.pdf_sha256 = expected_hash
    rma_record.pdf_validation_status = "passed"
    rma_record.pdf_archive_status = "archived"
    rma_record.pdf_archived_at = now
    rma_record.status = "issued"
    rma_record.issued_at = now
    ticket.rma_status = "issued"
    if ticket.current_status_code == "rma_sent":
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="closed",
            trigger_event="rma_issued_and_archived",
            user_id=user_id,
            operator_type="system" if auto else "user",
            reason="RMA回复发送成功且PDF与出站EML归档核验完成。",
            metadata={"reply_id": reply.id, "smtp_message_id": reply.smtp_message_id},
        )
    return True


async def retry_rma_archive(
    session: AsyncSession,
    *,
    reply_id: int,
    user_id: int | None,
) -> dict[str, Any]:
    reply = await session.get(ReplyRecord, reply_id, with_for_update=True)
    if reply is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="REPLY_NOT_FOUND")
    if reply.reply_type != "rma_authorization" or reply.send_status != "sent":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="RMA_ARCHIVE_RETRY_NOT_ALLOWED")
    ticket = await session.get(RepairTicket, reply.ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")
    if ticket.current_status_code == "closed":
        issued_rma = await session.scalar(
            select(TicketRma)
            .where(
                TicketRma.ticket_id == ticket.id,
                TicketRma.status == "issued",
                TicketRma.issued_at.is_not(None),
            )
            .order_by(TicketRma.id.desc())
        )
        if issued_rma is not None:
            ticket.rma_status = "issued"
        return {
            "status": "closed",
            "ticket_id": ticket.id,
            "reply_id": reply.id,
            "idempotent_reuse": True,
        }
    operation = await start_external_operation(
        session,
        operation_type="rma_archive_finalize",
        operation_key=f"reply:{reply.id}:archive-finalize",
        ticket_id=ticket.id,
        email_id=reply.related_email_id,
        reply_record_id=reply.id,
        recovery_stage="rma_archive_finalize",
    )
    attempt_before = int(reply.archive_attempt_count or 0)
    try:
        if reply.outgoing_email_id is None:
            raw_operation = await get_external_operation(
                session,
                operation_type="oss_put_outbound_eml",
                operation_key=f"reply:{reply.id}:raw-eml",
            )
            raw_object_id = int(raw_operation.remote_reference) if raw_operation and raw_operation.remote_reference else 0
            raw_hash = str((raw_operation.details_json or {}).get("sha256") or "") if raw_operation else ""
            if not raw_object_id or not raw_hash or not reply.smtp_message_id:
                raise ValueError("OUTBOUND_ARCHIVE_EVIDENCE_MISSING")
            attachment_content = await download_oss_object_bytes(
                session,
                oss_object_id=reply.rma_pdf_oss_object_id,
            )
            pdf_object = await session.get(OssObject, reply.rma_pdf_oss_object_id)
            await _archive_outbound_email(
                session,
                reply=reply,
                ticket=ticket,
                smtp_message_id=reply.smtp_message_id,
                raw_eml_oss_object_id=raw_object_id,
                raw_eml_sha256=raw_hash,
                attachment_oss_object_id=reply.rma_pdf_oss_object_id,
                attachment_content=attachment_content,
                attachment_filename=(
                    pdf_object.original_file_name
                    if pdf_object and pdf_object.original_file_name
                    else None
                ),
            )
        closed = await _finalize_rma_issue(
            session,
            ticket=ticket,
            reply=reply,
            user_id=user_id,
            auto=False,
        )
        if not closed:
            raise ValueError(reply.last_error_code or "RMA_ARCHIVE_FINALIZATION_FAILED")
        succeed_external_operation(
            operation,
            remote_reference=str(reply.outgoing_email_id),
            details={"ticket_status": ticket.current_status_code},
        )
        return {
            "status": ticket.current_status_code,
            "ticket_id": ticket.id,
            "reply_id": reply.id,
            "idempotent_reuse": False,
        }
    except Exception as exc:
        reply.archive_status = "archive_failed"
        if int(reply.archive_attempt_count or 0) == attempt_before:
            reply.archive_attempt_count = attempt_before + 1
        reply.last_error_code = str(exc)[:100]
        reply.error_message = str(exc)[:1000]
        retryable = int(reply.archive_attempt_count or 0) < 3
        reply.next_retry_at = (
            utcnow()
            + timedelta(
                minutes=(5, 15, 60)[
                    min(max(int(reply.archive_attempt_count or 1) - 1, 0), 2)
                ]
            )
            if retryable
            else None
        )
        fail_external_operation(
            operation,
            error_code=reply.last_error_code,
            error_message=reply.error_message,
            retryable=retryable,
            recovery_stage="rma_archive_finalize",
            next_retry_at=reply.next_retry_at,
        )
        await _ensure_reply_manual_task(
            session,
            ticket=ticket,
            task_type="rma_archive_failed",
            reason=reply.last_error_code,
            email_id=reply.related_email_id,
            user_id=user_id,
        )
        return {
            "status": "archive_failed",
            "ticket_id": ticket.id,
            "reply_id": reply.id,
            "error_code": reply.last_error_code,
        }


async def _enqueue_rma_archive_retry(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    reply: ReplyRecord,
    user_id: int | None,
) -> None:
    """Queue archive-only recovery after SMTP acceptance.

    The job never calls SMTP.  Its stable idempotency key ensures that one
    accepted RMA reply has at most one automatic archive recovery chain.
    """
    from app.services.jobs import enqueue_job

    reply.next_retry_at = utcnow()
    await enqueue_job(
        session,
        job_type="rma_archive",
        resource_type="reply_record",
        resource_id=reply.id,
        idempotency_key=f"rma_archive:{reply.id}",
        metadata={"user_id": user_id} if user_id is not None else {},
        max_attempts=3,
    )


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
    if ticket.current_status_code in {"ready_for_export", "rma_sent"}:
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


async def _reply_send_guard_error(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    reply: ReplyRecord,
) -> str | None:
    if ticket.ticket_category == "manual_business" and reply.reply_type == "rma_authorization":
        return "MANUAL_BUSINESS_RMA_FORBIDDEN"
    if is_followup_reply_type(reply.reply_type):
        ticket_items = list(
            (
                await session.execute(
                    select(RepairTicketItem).where(RepairTicketItem.ticket_id == ticket.id)
                )
            ).scalars().all()
        )
        current_missing = required_missing_for_ticket(ticket, ticket_items)
        if not current_missing:
            return "FOLLOWUP_NO_LONGER_REQUIRED"
        if current_missing != (reply.missing_fields or {}):
            return "FOLLOWUP_MISSING_FIELDS_CHANGED_REGENERATE_REQUIRED"
    if reply.template_id is None:
        return "REPLY_TEMPLATE_REQUIRED"
    template = await session.get(ReplyTemplate, reply.template_id)
    if template is None or not template.enabled:
        return "REPLY_TEMPLATE_NOT_AVAILABLE"
    if template.language == "zh-CN":
        if reply.base_template_id is None:
            return "REPLY_BASE_TEMPLATE_REQUIRED"
        base_template = await session.get(ReplyTemplate, reply.base_template_id)
        if (
            base_template is None
            or not base_template.enabled
            or base_template.template_type not in {
                "domestic_company_base", "international_company_base", "neutral_base"
            }
        ):
            return "REPLY_BASE_TEMPLATE_NOT_AVAILABLE"
    if reply.related_email_id is None:
        return "REPLY_PARENT_EMAIL_REQUIRED"
    parent = await session.get(Email, reply.related_email_id)
    parent_error = await _reply_parent_error(session, ticket=ticket, candidate=parent)
    if parent_error is not None:
        return parent_error
    thread = await session.get(EmailThread, ticket.thread_id) if ticket.thread_id else None
    if (
        reply.thread_version is not None
        and thread is not None
        and reply.thread_version != thread.thread_version
    ):
        return "REPLY_THREAD_CHANGED_REGENERATE_REQUIRED"
    try:
        current_history = await render_reply_history(
            session,
            parent=parent,
            language=template.language,
        )
    except ReplyRenderError as exc:
        return exc.code
    if not reply.thread_history_hash or reply.thread_history_hash != current_history.snapshot_hash:
        return "REPLY_THREAD_HISTORY_CHANGED_REGENERATE_REQUIRED"
    parent_message_id = _message_id_chain(parent.message_id)
    if _message_id_chain(reply.in_reply_to) != parent_message_id:
        return "REPLY_IN_REPLY_TO_INVALID"
    if parent_message_id not in (_message_id_chain(reply.references_header) or ""):
        return "REPLY_REFERENCES_INVALID"
    if reply.reply_type == "rma_authorization":
        rma_record = await session.scalar(
            select(TicketRma).where(TicketRma.ticket_id == ticket.id)
        )
        pdf_object = (
            await session.get(OssObject, reply.rma_pdf_oss_object_id)
            if reply.rma_pdf_oss_object_id
            else None
        )
        expected_subject = (
            str(pdf_object.original_file_name).removesuffix(".pdf")
            if pdf_object is not None and pdf_object.original_file_name
            else (
                f"RMA{rma_record.rma_no}{ticket.customer_name or ''}"[:500]
                if rma_record is not None
                else None
            )
        )
    else:
        expected_subject = _reply_subject(parent.subject, ticket.ticket_no)
    if not expected_subject or reply.subject != expected_subject:
        return "REPLY_SUBJECT_NOT_ORIGINAL_THREAD"
    if reply.final_body not in {None, reply.draft_body}:
        return "REPLY_TEMPLATE_BODY_MODIFIED"
    if reply.final_html_body not in {None, reply.draft_html_body}:
        return "REPLY_TEMPLATE_HTML_BODY_MODIFIED"
    expected_render_hash = _render_hash(
        subject=reply.subject,
        plain=reply.final_body or reply.draft_body,
        html_body=reply.final_html_body or reply.draft_html_body,
        history_hash=reply.thread_history_hash,
    )
    if not reply.render_hash or reply.render_hash != expected_render_hash:
        return "REPLY_RENDER_EVIDENCE_INVALID"
    return None


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

    def sync_ticket_delivery_status() -> None:
        if reply.reply_type == "rma_authorization" and reply.send_status in {"send_failed", "send_uncertain"}:
            ticket.rma_status = "manual_review"

    if reply.send_status == "sent" and reply.smtp_message_id:
        await _finalize_rma_issue(
            session,
            ticket=ticket,
            reply=reply,
            user_id=user_id,
            auto=auto,
        )
        return
    reply.send_attempt_count = int(reply.send_attempt_count or 0) + 1
    guard_error = await _reply_send_guard_error(session, ticket=ticket, reply=reply)
    if guard_error is not None:
        reply.send_status = "send_failed"
        reply.last_error_code = guard_error
        reply.error_message = guard_error
        sync_ticket_delivery_status()
        await _ensure_reply_manual_task(
            session,
            ticket=ticket,
            task_type="reply_send_blocked",
            reason=guard_error,
            email_id=reply.related_email_id,
            user_id=user_id,
        )
        return
    if reply.send_status in {"sending", "auto_sending", "send_uncertain"}:
        reply.send_status = "send_uncertain"
        reply.last_error_code = "SMTP_SEND_RESULT_UNCERTAIN"
        reply.error_message = "SMTP_SEND_RESULT_UNCERTAIN"
        sync_ticket_delivery_status()
        await _ensure_reply_manual_task(
            session, ticket=ticket, task_type="reply_send_uncertain",
            reason="SMTP_SEND_RESULT_UNCERTAIN", email_id=reply.related_email_id, user_id=user_id,
        )
        return
    if not _smtp_sender_is_exact_login():
        reply.send_status = "send_failed"
        reply.last_error_code = "SMTP_SENDER_LOGIN_MISMATCH"
        reply.error_message = "SMTP_SENDER_LOGIN_MISMATCH"
        sync_ticket_delivery_status()
        await _ensure_reply_manual_task(
            session, ticket=ticket, task_type="reply_send_failed",
            reason=reply.error_message, email_id=reply.related_email_id, user_id=user_id,
        )
        return
    if not _rma_envelope_valid(reply):
        reply.send_status = "send_failed"
        reply.last_error_code = "SMTP_TEST_ENVELOPE_INVALID"
        reply.error_message = "SMTP_TEST_ENVELOPE_INVALID"
        sync_ticket_delivery_status()
        await _ensure_reply_manual_task(
            session, ticket=ticket, task_type="reply_send_failed",
            reason=reply.error_message, email_id=reply.related_email_id, user_id=user_id,
        )
        return
    if not _recipient_in_whitelist(reply.to_addresses, reply.cc_addresses):
        reply.send_status = "send_failed"
        reply.last_error_code = "SMTP_RECIPIENT_NOT_ALLOWED"
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
    template = await session.get(ReplyTemplate, reply.template_id) if reply.template_id else None
    parent = await session.get(Email, reply.related_email_id) if reply.related_email_id else None
    try:
        if template is None or parent is None:
            raise ReplyRenderError("REPLY_RENDER_PREREQUISITE_MISSING")
        reply_history = await render_reply_history(
            session,
            parent=parent,
            language=template.language,
        )
        if reply_history.snapshot_hash != reply.thread_history_hash:
            raise ReplyRenderError("REPLY_THREAD_HISTORY_CHANGED_REGENERATE_REQUIRED")
    except ReplyRenderError as exc:
        reply.send_status = "send_failed"
        reply.last_error_code = exc.code
        reply.error_message = exc.code
        sync_ticket_delivery_status()
        await _ensure_reply_manual_task(
            session,
            ticket=ticket,
            task_type="reply_render_failed",
            reason=exc.code,
            email_id=reply.related_email_id,
            user_id=user_id,
        )
        return
    message_id = _smtp_message_id(reply)
    reply.smtp_message_id = message_id
    outbound_message = _build_reply_message(
        reply,
        message_id,
        related_resources=reply_history.resources,
        attachment_content=attachment_content,
        attachment_filename=attachment_filename,
    )
    raw_message = outbound_message.as_bytes()
    raw_hash = hashlib.sha256(raw_message).hexdigest()
    archive_operation = None
    try:
        archive_operation = await start_external_operation(
            session,
            operation_type="oss_put_outbound_eml",
            operation_key=f"reply:{reply.id}:raw-eml",
            ticket_id=ticket.id,
            email_id=reply.related_email_id,
            reply_record_id=reply.id,
            recovery_stage="outbound_archive",
        )
        if archive_operation.status == "succeeded" and archive_operation.remote_reference:
            raw_object = await session.get(
                OssObject,
                int(archive_operation.remote_reference),
            )
            archived_hash = str(
                (archive_operation.details_json or {}).get("sha256") or ""
            )
            if raw_object is None or archived_hash != raw_hash:
                raise StorageUploadError("OUTBOUND_ARCHIVE_EVIDENCE_MISMATCH")
        else:
            raw_object = await upload_bytes_to_oss(
                session,
                content=raw_message,
                original_file_name=f"reply-{reply.id}.eml",
                content_type="message/rfc822",
                source_type="outbound_raw_eml",
                user_id=user_id,
            )
            succeed_external_operation(
                archive_operation,
                remote_reference=str(raw_object.id),
                details={"sha256": raw_hash},
            )
    except (StorageConfigurationError, StorageUploadError) as exc:
        reply.send_status = "send_failed"
        reply.archive_status = "archive_failed"
        reply.archive_attempt_count = int(reply.archive_attempt_count or 0) + 1
        reply.last_error_code = "OUTBOUND_ARCHIVAL_FAILED"
        reply.error_message = "OUTBOUND_ARCHIVAL_FAILED"
        if archive_operation is not None:
            fail_external_operation(
                archive_operation,
                error_code="OUTBOUND_ARCHIVAL_FAILED",
                error_message=str(exc),
                retryable=True,
                recovery_stage="outbound_archive",
            )
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

    smtp_operation = await start_external_operation(
        session,
        operation_type="smtp_send",
        operation_key=f"reply:{reply.id}:smtp",
        ticket_id=ticket.id,
        email_id=reply.related_email_id,
        reply_record_id=reply.id,
        recovery_stage="smtp_send",
    )
    reply.archive_status = "archive_pending" if reply.reply_type == "rma_authorization" else reply.archive_status
    if smtp_operation.status == "succeeded" and smtp_operation.remote_reference:
        # SMTP acceptance is already durably known.  Recover local state and
        # archive records without issuing another customer email.
        ok = True
        sent_message_id = str(smtp_operation.remote_reference)
        error = None
    else:
        reply.send_status = "auto_sending" if auto else "sending"
        await _commit_if_available(session)
        async with _smtp_semaphore:
            ok, sent_message_id, error = await asyncio.to_thread(
                _send_reply_via_smtp,
                reply,
                message=outbound_message,
            )
    if ok and sent_message_id:
        was_counted = reply.send_status == "sent"
        reply.send_status = "sent"
        reply.sent_at = utcnow()
        reply.smtp_response = "SMTP_ACCEPTED"
        reply.last_error_code = None
        reply.error_message = None
        succeed_external_operation(
            smtp_operation,
            remote_reference=sent_message_id,
            details={"accepted": True},
        )
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
            if ticket.current_status_code == "ready_for_export":
                await transition_ticket(
                    session,
                    ticket=ticket,
                    to_status_code="rma_sent",
                    trigger_event="rma_reply_sent",
                    user_id=user_id,
                    operator_type="system" if auto else "user",
                    reason="SAP RMA编号已回填，RMA模板回复已在原邮件链发送成功。",
                    metadata={"reply_id": reply.id, "smtp_message_id": sent_message_id},
                )
            rma_record = await session.scalar(
                select(TicketRma).where(TicketRma.ticket_id == ticket.id)
            )
            if rma_record is not None:
                rma_record.status = "sent"
                rma_record.reply_record_id = reply.id
                rma_record.sent_at = utcnow()
            await resolve_completed_rma_tasks(
                session,
                ticket_id=ticket.id,
                user_id=user_id,
            )
            await notify_ticket_once(
                session,
                ticket=ticket,
                event_type="rma_reply_sent",
                title="RMA 已成功发送",
                content=f"工单 {ticket.ticket_no} 的 RMA 模板回复已在原邮件链发送成功。",
                metadata={
                    "reply_id": reply.id,
                    "rma_no": rma_record.rma_no if rma_record is not None else None,
                },
            )
            closed = await _finalize_rma_issue(
                session,
                ticket=ticket,
                reply=reply,
                user_id=user_id,
                auto=auto,
            )
            if not closed:
                await _enqueue_rma_archive_retry(
                    session,
                    ticket=ticket,
                    reply=reply,
                    user_id=user_id,
                )
        if is_followup_reply_type(reply.reply_type) and not was_counted:
            ticket.followup_count = min(ticket.max_followup_count, ticket.followup_count + 1)
        if is_followup_reply_type(reply.reply_type) and ticket.current_status_code == "need_customer_info":
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
        reply.last_error_code = error or "SMTP_SEND_FAILED"
        reply.error_message = error or "SMTP_SEND_FAILED"
        fail_external_operation(
            smtp_operation,
            error_code=reply.last_error_code,
            error_message=reply.error_message,
            retryable=reply.send_status == "send_failed",
            uncertain=reply.send_status == "send_uncertain",
            recovery_stage="smtp_send",
        )
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
    if ticket.ticket_category == "manual_business" and reply_kind == "rma_authorization":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="MANUAL_BUSINESS_RMA_FORBIDDEN")
    language = "en-US" if ticket.language_code == "en-US" else "zh-CN"
    if is_followup_reply_type(reply_kind) and ticket.current_status_code != "need_customer_info":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="FOLLOWUP_TICKET_NOT_WAITING_CUSTOMER_INFO")
    try:
        related_email = await _require_reply_parent(
            session,
            ticket=ticket,
            related_email_id=related_email_id,
        )
    except HTTPException as exc:
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="reply_parent_required",
            trigger_reason=str(exc.detail),
            email_id=related_email_id,
            priority="high",
        )
        raise
    effective_related_email_id = related_email.id
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
        can_auto_send = _reply_can_auto_send(existing_draft)
        if can_auto_send:
            existing_draft.review_status = "auto_approved"
            existing_draft.reviewed_at = utcnow()
            await _send_reply_record(
                session,
                reply=existing_draft,
                user_id=user_id,
                auto=True,
            )
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

    if is_followup_reply_type(reply_kind):
        ticket_items = list(
            (
                await session.execute(
                    select(RepairTicketItem).where(RepairTicketItem.ticket_id == ticket.id)
                )
            ).scalars().all()
        )
        effective_missing_fields = required_missing_for_ticket(ticket, ticket_items)
        if not effective_missing_fields:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="FOLLOWUP_NO_LONGER_REQUIRED")
    else:
        effective_missing_fields = missing_fields if missing_fields is not None else ticket.missing_fields
    if template is None:
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="reply_template_missing",
            trigger_reason=f"REPLY_TEMPLATE_NOT_FOUND:{reply_kind}:{language}",
            email_id=related_email.id,
            priority="high",
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="REPLY_TEMPLATE_NOT_FOUND")
    try:
        subject, body, html_body, base_template, history_hash, render_hash = await _render_reply_templates(
            session,
            content_template=template,
            ticket=ticket,
            missing_fields=effective_missing_fields,
            parent=related_email,
        )
    except ReplyRenderError as exc:
        await _ensure_reply_manual_task(
            session,
            ticket=ticket,
            task_type="reply_render_failed",
            reason=exc.code,
            email_id=related_email.id,
            user_id=user_id,
        )
        await _commit_if_available(session)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc
    except HTTPException as exc:
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="reply_base_template_missing",
            trigger_reason=str(exc.detail),
            email_id=related_email.id,
            priority="high",
        )
        raise
    generate_source = "template"
    ai_call_log_id: int | None = None
    reply_confidence_score: float | None = None
    reply_risk_level: str | None = None
    reply = ReplyRecord(
        ticket_id=ticket.id,
        related_email_id=related_email.id if related_email else None,
        template_id=template.id,
        base_template_id=base_template.id if base_template else None,
        reply_type=reply_kind,
        followup_round=(ticket.followup_count + 1) if is_followup_reply_type(reply_kind) else ticket.followup_count,
        missing_fields=effective_missing_fields,
        to_addresses=TEST_MAIL_RECIPIENT,
        cc_addresses=None,
        subject=subject,
        draft_body=body,
        final_body=body,
        draft_html_body=html_body,
        final_html_body=html_body,
        thread_history_hash=history_hash,
        render_hash=render_hash,
        generate_source=generate_source,
        ai_call_log_id=ai_call_log_id,
        review_status="pending",
        send_status="pending_review",
        thread_version=await _current_thread_version(session, ticket),
        in_reply_to=_message_id_chain(related_email.message_id),
        references_header=_message_id_chain(
            related_email.references_header,
            related_email.in_reply_to,
            related_email.message_id,
        ),
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
    allowed = {"to_addresses", "cc_addresses"}
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
        return {"status": "sent", "reply": serialize_reply(reply), "auto_send_enabled": settings.AUTO_SEND_ENABLED, "reply_send_mode": settings.REPLY_SEND_MODE}
    if reply.send_status in {"sending", "auto_sending", "send_uncertain"}:
        await _send_reply_record(session, reply=reply, user_id=user_id, auto=False)
        return {
            "status": reply.send_status,
            "error_code": reply.last_error_code or reply.error_message,
            "reply": serialize_reply(reply),
            "auto_send_enabled": settings.AUTO_SEND_ENABLED,
            "reply_send_mode": settings.REPLY_SEND_MODE,
        }
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
    return {
        "status": reply.send_status,
        "error_code": reply.last_error_code or reply.error_message,
        "reply": serialize_reply(reply),
        "auto_send_enabled": settings.AUTO_SEND_ENABLED,
        "reply_send_mode": settings.REPLY_SEND_MODE,
    }


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
            if ticket.current_status_code == "need_customer_info":
                await transition_ticket(
                    session,
                    ticket=ticket,
                    to_status_code="auto_replied",
                    trigger_event="reply_sent",
                    user_id=user_id,
                    operator_type="user",
                    reason="人工确认结果不确定的追问邮件实际已成功发送。",
                    metadata={
                        "reply_id": reply.id,
                        "smtp_message_id": reply.smtp_message_id,
                        "reconciled": True,
                    },
                )
        elif reply.reply_type == "rma_authorization":
            ticket.rma_status = "sent"
            if ticket.current_status_code == "ready_for_export":
                await transition_ticket(
                    session,
                    ticket=ticket,
                    to_status_code="rma_sent",
                    trigger_event="rma_reply_sent",
                    user_id=user_id,
                    operator_type="user",
                    reason="人工确认RMA回复实际发送成功。",
                    metadata={"reply_id": reply.id, "reconciled": True},
                )
            rma_record = await session.scalar(
                select(TicketRma).where(TicketRma.ticket_id == ticket.id)
            )
            if rma_record is not None:
                rma_record.status = "sent"
                rma_record.reply_record_id = reply.id
                rma_record.sent_at = utcnow()
            await retry_rma_archive(
                session,
                reply_id=reply.id,
                user_id=user_id,
            )
    elif outcome == "failed":
        reply.send_status = "send_failed"
        reply.error_message = "SMTP_SEND_CONFIRMED_FAILED"
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
    recovery_actions = {
        "rma_special_policy_review": (
            "核对客户政策中的特殊价格、币种、称呼或匿名要求；当前版本禁止自动签发。"
            "人工必须从原邮件线程处理，使用模板正文，记录最终价格/PDF、Message-ID、发送结果和归档结果。"
        ),
        "rma_amkor_manual": (
            "按客户定制要求人工核对正文和附件；从原邮件线程发送并登记 Message-ID 与归档证据。"
        ),
        "rma_st_manual": (
            "按 ST 客户定制要求人工核对正文、称呼及公司名称展示；"
            "从原邮件线程发送并登记 Message-ID 与归档证据。"
        ),
        "rma_price_required": (
            "由业务人员确认超保价格和币种后更新客户政策；不得猜测价格，确认后重新执行 RMA 安全校验。"
        ),
    }
    await create_manual_task_if_missing(
        session,
        ticket=ticket,
        task_type=task_type,
        trigger_reason=reason,
        priority="high",
        assigned_user_id=ticket.assigned_user_id,
        recovery_stage="rma_manual_review",
        recovery_action=recovery_actions.get(
            task_type,
            "核对异常根因并在原业务节点恢复；不得绕过模板、线程、安全校验或外部操作记录。",
        ),
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
    expected_rma_no: str = "",
) -> dict[str, Any]:
    ticket = await session.get(RepairTicket, ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")
    if not ticket.rma_required:
        ticket.rma_status = "not_required"
        return {"status": "not_required", "ticket_id": ticket.id}
    if ticket.current_status_code == "closed":
        sent_reply = await session.scalar(
            select(ReplyRecord)
            .where(
                ReplyRecord.ticket_id == ticket.id,
                ReplyRecord.reply_type == "rma_authorization",
                ReplyRecord.send_status == "sent",
            )
            .order_by(ReplyRecord.id.desc())
        )
        return {
            "status": "closed",
            "ticket_id": ticket.id,
            "reply_id": sent_reply.id if sent_reply is not None else None,
            "idempotent_reuse": True,
        }
    if ticket.current_status_code == "rma_sent":
        sent_reply = await session.scalar(
            select(ReplyRecord)
            .where(
                ReplyRecord.ticket_id == ticket.id,
                ReplyRecord.reply_type == "rma_authorization",
                ReplyRecord.send_status == "sent",
            )
            .order_by(ReplyRecord.id.desc())
        )
        sent_rmas = list(
            (
                await session.execute(
                    select(TicketRma).where(TicketRma.ticket_id == ticket.id)
                )
            ).scalars().all()
        )
        if (
            sent_reply is not None
            and len(sent_rmas) == 1
            and (
                not expected_rma_no
                or sent_rmas[0].rma_no == expected_rma_no
            )
        ):
            ticket.rma_status = "sent"
            sent_rmas[0].status = "issued" if ticket.current_status_code == "closed" else "sent"
            sent_rmas[0].reply_record_id = sent_reply.id
            sent_rmas[0].sent_at = sent_reply.sent_at or utcnow()
            if ticket.current_status_code == "rma_sent":
                return await retry_rma_archive(
                    session,
                    reply_id=sent_reply.id,
                    user_id=user_id,
                )
            return {
                "status": "closed",
                "ticket_id": ticket.id,
                "reply_id": sent_reply.id,
                "idempotent_reuse": True,
            }
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
    rma_records = list(
        (
            await session.execute(
                select(TicketRma).where(TicketRma.ticket_id == ticket.id)
            )
        ).scalars().all()
    )
    if len(rma_records) != 1 or (
        expected_rma_no and rma_records[0].rma_no != expected_rma_no
    ):
        return await _rma_manual_review(
            session,
            ticket=ticket,
            task_type="rma_number_not_unique",
            reason="RMA_NUMBER_NOT_UNIQUE_FOR_TICKET",
        )
    rma_record = rma_records[0]
    policy_lines = list((rma_record.policy_snapshot or {}).get("lines") or [])
    branding_rules = {
        (
            str(line.get("reply_salutation") or "").strip(),
            bool(line.get("hide_company_name")),
        )
        for line in policy_lines
        if isinstance(line, dict)
    }
    if len(branding_rules) > 1:
        return await _rma_manual_review(
            session,
            ticket=ticket,
            task_type="rma_branding_policy_conflict",
            reason="RMA_BRANDING_POLICY_CONFLICT",
        )
    customer_policy = policy_lines[0] if policy_lines else {}
    attach_rma = bool(settings.RMA_AUTO_SEND_ENABLED)
    manual_special_reasons: list[str] = []
    if str(customer_policy.get("policy_type") or "") == "special_out_of_warranty":
        manual_special_reasons.append("SPECIAL_OUT_OF_WARRANTY_PRICE")
    if str(customer_policy.get("currency") or "RMB").upper() not in {"RMB", "CNY"}:
        manual_special_reasons.append("NON_RMB_CURRENCY")
    if str(customer_policy.get("reply_salutation") or "").strip():
        manual_special_reasons.append("SPECIAL_REPLY_SALUTATION")
    if bool(customer_policy.get("hide_company_name")):
        manual_special_reasons.append("ANONYMOUS_REPLY")
    if bool(customer_policy.get("force_manual_review")):
        manual_special_reasons.append("CUSTOMER_POLICY_FORCES_MANUAL_REVIEW")
    if manual_special_reasons and not bool(customer_policy.get("manual_approved")):
        return await _rma_manual_review(
            session,
            ticket=ticket,
            task_type="rma_special_policy_review",
            reason="RMA_SPECIAL_POLICY_REQUIRES_MANUAL:" + ",".join(manual_special_reasons),
        )
    manual_send_only = bool(
        customer_policy.get("manual_approved")
        or (rma_record.policy_snapshot or {}).get("manual_send_only")
    )
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
            if ticket.current_status_code == "ready_for_export":
                await transition_ticket(
                    session,
                    ticket=ticket,
                    to_status_code="rma_sent",
                    trigger_event="rma_reply_sent",
                    user_id=user_id,
                    operator_type="system",
                    reason="复用已成功发送的 RMA 模板回复并恢复工单主状态。",
                    metadata={
                        "reply_id": existing.id,
                        "smtp_message_id": existing.smtp_message_id,
                        "idempotent_reuse": True,
                    },
                )
            rma_record.status = "sent"
            rma_record.reply_record_id = existing.id
            rma_record.sent_at = existing.sent_at or utcnow()
            await notify_ticket_once(
                session,
                ticket=ticket,
                event_type="rma_reply_sent",
                title="RMA 已成功发送",
                content=f"工单 {ticket.ticket_no} 的 RMA 模板回复已在原邮件链发送成功。",
                metadata={"reply_id": existing.id, "rma_no": rma_record.rma_no},
            )
            if ticket.current_status_code == "rma_sent":
                return await retry_rma_archive(
                    session,
                    reply_id=existing.id,
                    user_id=user_id,
                )
        return {"status": existing.send_status, "ticket_id": ticket.id, "reply_id": existing.id, "idempotent_reuse": True}

    pdf_content: bytes | None = None
    pdf_object: OssObject | None = None
    data = None
    reply_language = "en-US" if ticket.language_code == "en-US" else "zh-CN"
    try:
        related_email = await _require_reply_parent(session, ticket=ticket)
    except HTTPException as exc:
        return await _rma_manual_review(
            session,
            ticket=ticket,
            task_type="rma_reply_parent_required",
            reason=str(exc.detail),
        )
    if attach_rma:
        try:
            template_type, reply_template_version = _rma_reply_template_type(ticket)
        except RmaReplyRuleError as exc:
            return await _rma_manual_review(session, ticket=ticket, task_type=exc.task_type, reason=exc.reason)
        template = await _select_template(session, template_type, reply_language)
        if template is None:
            return await _rma_manual_review(
                session,
                ticket=ticket,
                task_type="rma_reply_template_missing",
                reason=f"REPLY_TEMPLATE_NOT_FOUND:{template_type}:{reply_language}",
            )
    else:
        template = await _select_template(session, "rma_attachment_disabled_receipt", reply_language)
        if template is None:
            return await _rma_manual_review(
                session,
                ticket=ticket,
                task_type="rma_reply_template_missing",
                reason=f"REPLY_TEMPLATE_NOT_FOUND:rma_attachment_disabled_receipt:{reply_language}",
            )
        reply_template_version = template.version
    try:
        subject, body, html_body, base_template, history_hash, render_hash = await _render_reply_templates(
            session,
            content_template=template,
            ticket=ticket,
            missing_fields=None,
            parent=related_email,
            customer_policy=customer_policy,
        )
    except ReplyRenderError as exc:
        return await _rma_manual_review(
            session,
            ticket=ticket,
            task_type="rma_reply_render_failed",
            reason=exc.code,
        )
    except HTTPException as exc:
        return await _rma_manual_review(
            session,
            ticket=ticket,
            task_type="rma_reply_base_template_missing",
            reason=str(exc.detail),
        )
    if attach_rma:
        ticket.rma_status = "generating"
        try:
            data = await build_rma_pdf_data(
                session,
                ticket_id=ticket.id,
                safety_snapshot=ticket.safety_check_snapshot,
                rma_no=rma_record.rma_no,
            )
            pdf_content = await asyncio.to_thread(render_rma_pdf, data, test_only=False)
            file_name = rma_pdf_file_name(data)
            subject = file_name.removesuffix(".pdf")
            render_hash = _render_hash(
                subject=subject,
                plain=body,
                html_body=html_body,
                history_hash=history_hash,
            )
            pdf_object = await upload_bytes_to_oss(
                session,
                content=pdf_content,
                original_file_name=file_name,
                content_type="application/pdf",
                source_type="rma_authorization_pdf",
                user_id=user_id,
            )
            rma_record.pdf_oss_object_id = pdf_object.id
            rma_record.pdf_sha256 = hashlib.sha256(pdf_content).hexdigest()
            rma_record.pdf_validation_status = "passed"
            rma_record.pdf_archive_status = "staged"
        except (RmaPdfError, StorageConfigurationError, StorageUploadError) as exc:
            return await _rma_manual_review(session, ticket=ticket, task_type="rma_generation_failed", reason=str(exc)[:100])
    reply = ReplyRecord(
        ticket_id=ticket.id,
        related_email_id=related_email.id if related_email else None,
        template_id=template.id,
        base_template_id=base_template.id if base_template else None,
        reply_type=reply_type,
        followup_round=ticket.followup_count,
        to_addresses=TEST_MAIL_RECIPIENT,
        cc_addresses=None,
        subject=subject,
        draft_body=body,
        final_body=body,
        draft_html_body=html_body,
        final_html_body=html_body,
        thread_history_hash=history_hash,
        render_hash=render_hash,
        generate_source="template",
        rma_pdf_oss_object_id=pdf_object.id if pdf_object else None,
        reply_template_version=reply_template_version,
        rma_template_version=RMA_TEMPLATE_VERSION if attach_rma else None,
        rma_pdf_data_snapshot=(
            rma_pdf_snapshot(data, pdf_content=pdf_content, oss_object_id=pdf_object.id)
            if data is not None and pdf_content is not None and pdf_object is not None
            else None
        ),
        review_status=(
            "auto_approved"
            if settings.AUTO_SEND_ENABLED and not manual_send_only
            else "pending"
        ),
        reviewed_at=(
            utcnow()
            if settings.AUTO_SEND_ENABLED and not manual_send_only
            else None
        ),
        send_status=(
            "approved_pending_send"
            if settings.AUTO_SEND_ENABLED and not manual_send_only
            else "pending_review"
        ),
        thread_version=await _current_thread_version(session, ticket),
        in_reply_to=_message_id_chain(related_email.message_id),
        references_header=_message_id_chain(
            related_email.references_header,
            related_email.in_reply_to,
            related_email.message_id,
        ),
    )
    session.add(reply)
    await session.flush()
    rma_record.reply_record_id = reply.id
    if not settings.AUTO_SEND_ENABLED or manual_send_only:
        ticket.rma_status = "manual_review"
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="rma_reply_review" if attach_rma else "rma_attachment_disabled",
            trigger_reason=(
                "特殊客户政策已由人工确认，RMA PDF 和模板回复已生成；"
                "必须由操作员在系统内复核后批准发送。"
                if manual_send_only
                else "普通回复自动发送已关闭，需要人工审核新报修回复。"
            ),
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
