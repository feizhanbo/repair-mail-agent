from __future__ import annotations

import re
from datetime import date
from email.utils import parseaddr
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.request_context import get_correlation_id
from app.core.email_classification import (
    AUTO_INTENTS,
    CLASSIFICATION_VERSION,
    LIFECYCLE_INTENTS,
    MANUAL_INTENTS,
    EmailIntent,
    HandlingLevel,
    decision_for_intent,
    normalize_intent,
)
from app.models import (
    Email,
    EmailAttachment,
    EmailThread,
    EmailTicketLink,
    JobRunLog,
    ManualReviewTask,
    ParseResult,
    RepairTicket,
    WorkflowExecution,
)
from app.schemas.business import EmailIngestRequest
from app.services.ai import create_ai_parse_candidate
from app.services.attachment_parser import attachment_type, parse_attachment
from app.services.audit import log_operation, log_system_event
from app.services.common import address_domain, model_to_dict, normalize_message_id, normalize_subject, paginate_scalars, sha256_text, to_plain, utcnow
from app.services.jobs import enqueue_job
from app.services.parser import (
    RuleAnalysisResult,
    analyze_email_rules,
    extract_latest_reply_segment,
    html_to_text,
    normalize_email_body,
)
from app.services.replies import create_reply_draft
from app.services.ticket_safety import validate_and_mark_ready_for_export
from app.services.tickets import (
    EMAIL_FIELDS,
    apply_parse_result,
    create_manual_business_ticket_from_email,
    ensure_manual_review_ticket_from_parse_result,
    serialize_email,
    serialize_parse_result,
)
from app.services.workflow import OPEN_TASK_STATUSES, create_email_manual_task_if_missing, create_manual_task_if_missing


def attachment_file_size_kb(file_size: int | None) -> int | None:
    if file_size is None:
        return None
    return max(1, (int(file_size) + 1023) // 1024)


def serialize_attachment(attachment: EmailAttachment, email: Email | None = None) -> dict[str, Any]:
    data = model_to_dict(
        attachment,
        (
            "id",
            "email_id",
            "oss_object_id",
            "file_name",
            "content_type",
            "file_size",
            "file_hash",
            "is_inline",
            "content_id",
            "parse_status",
            "extracted_text",
            "extracted_json",
            "parse_error",
            "created_at",
        ),
    )
    data["file_size_kb"] = attachment_file_size_kb(attachment.file_size)
    data["sent_at"] = to_plain((email.sent_at or email.received_at) if email else None)
    return data


def _set_classification(
    email: Email,
    parse_result: ParseResult | None,
    *,
    intent_type: str | None,
    confidence: float | None,
    reason_code: str,
) -> str:
    intent = normalize_intent(intent_type)
    decision = decision_for_intent(intent, reason_code=reason_code)
    email.intent_type = decision.intent_type
    email.intent_subtype = None
    email.handling_level = decision.handling_level
    email.classification_version = CLASSIFICATION_VERSION
    email.classification_confidence = confidence
    email.classification_reason_code = reason_code
    if parse_result is not None:
        parse_result.intent_type = decision.intent_type

        parse_result.intent_subtype = None
        parse_result.handling_level = decision.handling_level
        parse_result.classification_version = CLASSIFICATION_VERSION
        parse_result.classification_confidence = confidence
        parse_result.classification_reason_code = reason_code
    return decision.intent_type


def _contextual_intent(
    email: Email,
    proposed_intent: str | None,
    thread_ticket: RepairTicket | None,
) -> tuple[str, str]:
    intent = normalize_intent(proposed_intent)
    has_reply_headers = bool(email.in_reply_to or email.references_header)
    active_standard = bool(
        thread_ticket
        and thread_ticket.ticket_category == "standard_repair"
        and thread_ticket.current_status_code not in {"closed", "resolved"}
    )
    if intent == EmailIntent.NEW_REPAIR and has_reply_headers:
        intent = str(EmailIntent.THREAD_NEW_REPAIR)
    if intent == EmailIntent.THREAD_NEW_REPAIR and active_standard:
        return str(EmailIntent.REPAIR_THREAD_OTHER), "ACTIVE_FIRST_BLOCKS_THREAD_NEW_REPAIR"
    if intent == EmailIntent.CUSTOMER_SUPPLEMENT:
        waiting = active_standard and thread_ticket.current_status_code in {
            "need_customer_info",
            "auto_replied",
            "manual_review",
        }
        if not waiting:
            return str(EmailIntent.REPAIR_THREAD_OTHER), "SUPPLEMENT_WITHOUT_WAITING_FIRST"
    return intent, "CONTEXTUAL_CLASSIFICATION_RESOLVED"


async def _link_lifecycle_email(
    session: AsyncSession,
    *,
    email: Email,
    ticket: RepairTicket | None,
    intent_type: str,
) -> None:
    if ticket is None:
        return
    existing = await session.scalar(
        select(EmailTicketLink.id).where(
            EmailTicketLink.email_id == email.id,
            EmailTicketLink.ticket_id == ticket.id,
            EmailTicketLink.link_type == "lifecycle_event",
        )
    )
    if existing is None:
        session.add(
            EmailTicketLink(
                email_id=email.id,
                ticket_id=ticket.id,
                link_type="lifecycle_event",
                link_reason=f"THIRD lifecycle-only classification: {intent_type}",
            )
        )


async def list_emails(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    parse_status: str | None = None,
    intent_type: str | None = None,
    intent_subtype: str | None = None,
    handling_level: str | None = None,
    keyword: str | None = None,
    subject: str | None = None,
    from_address: str | None = None,
    message_id: str | None = None,
    received_start: date | None = None,
    received_end: date | None = None,
) -> tuple[list[dict[str, Any]], int]:
    statement = select(Email)
    if parse_status:
        statement = statement.where(Email.parse_status == parse_status)
    if intent_type:
        statement = statement.where(Email.intent_type == intent_type)
    if intent_subtype:
        statement = statement.where(Email.intent_subtype == intent_subtype)
    if handling_level:
        statement = statement.where(Email.handling_level == handling_level)
    if subject:
        statement = statement.where(Email.subject.like(f"%{subject}%"))
    if from_address:
        statement = statement.where(Email.from_address.like(f"%{from_address}%"))
    if message_id:
        statement = statement.where(Email.message_id.like(f"%{message_id}%"))
    if received_start:
        statement = statement.where(Email.received_at >= received_start)
    if received_end:
        statement = statement.where(Email.received_at <= received_end)
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(or_(Email.subject.like(like), Email.from_address.like(like), Email.clean_body.like(like)))

    statement = statement.order_by(Email.received_at.desc(), Email.id.desc())
    emails, total = await paginate_scalars(session, statement, page, page_size)
    return [model_to_dict(email, EMAIL_FIELDS) for email in emails], total


async def export_emails(
    session: AsyncSession,
    *,
    parse_status: str | None = None,
    intent_type: str | None = None,
    intent_subtype: str | None = None,
    handling_level: str | None = None,
    keyword: str | None = None,
    subject: str | None = None,
    from_address: str | None = None,
    message_id: str | None = None,
    received_start: date | None = None,
    received_end: date | None = None,
) -> list[dict[str, Any]]:
    statement = select(Email)
    if parse_status:
        statement = statement.where(Email.parse_status == parse_status)
    if intent_type:
        statement = statement.where(Email.intent_type == intent_type)
    if intent_subtype:
        statement = statement.where(Email.intent_subtype == intent_subtype)
    if handling_level:
        statement = statement.where(Email.handling_level == handling_level)
    if subject:
        statement = statement.where(Email.subject.like(f"%{subject}%"))
    if from_address:
        statement = statement.where(Email.from_address.like(f"%{from_address}%"))
    if message_id:
        statement = statement.where(Email.message_id.like(f"%{message_id}%"))
    if received_start:
        statement = statement.where(Email.received_at >= received_start)
    if received_end:
        statement = statement.where(Email.received_at <= received_end)
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(or_(Email.subject.like(like), Email.from_address.like(like), Email.clean_body.like(like)))
    rows = (await session.execute(statement.order_by(Email.received_at.desc(), Email.id.desc()))).scalars().all()
    export_rows: list[dict[str, Any]] = []
    for email in rows:
        attachment_count = int(
            await session.scalar(select(func.count()).select_from(EmailAttachment).where(EmailAttachment.email_id == email.id)) or 0
        )
        latest_parse = await session.scalar(
            select(ParseResult).where(ParseResult.email_id == email.id).order_by(ParseResult.created_at.desc(), ParseResult.id.desc())
        )
        export_rows.append(
            {
                "id": email.id,
                "message_id": email.message_id,
                "subject": email.subject,
                "from_address": email.from_address,
                "to_addresses": email.to_addresses,
                "intent_type": email.intent_type,
                "intent_subtype": email.intent_subtype,
                "handling_level": email.handling_level,
                "classification_version": email.classification_version,
                "classification_confidence": email.classification_confidence,
                "classification_reason_code": email.classification_reason_code,
                "parse_status": email.parse_status,
                "received_at": email.received_at,
                "attachment_count": attachment_count,
                "latest_parser_type": latest_parse.parser_type if latest_parse else None,
                "latest_confidence_score": latest_parse.confidence_score if latest_parse else None,
                "latest_missing_fields": to_plain(latest_parse.missing_fields) if latest_parse else None,
                "latest_conflict_fields": to_plain(latest_parse.conflict_fields) if latest_parse else None,
            }
        )
    return export_rows


async def get_email_detail(session: AsyncSession, email_id: int) -> dict[str, Any]:
    email = await session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")
    attachments = (
        await session.execute(select(EmailAttachment).where(EmailAttachment.email_id == email.id).order_by(EmailAttachment.created_at.desc()))
    ).scalars().all()
    parse_results = (
        await session.execute(select(ParseResult).where(ParseResult.email_id == email.id).order_by(ParseResult.created_at.desc()))
    ).scalars().all()
    return {
        "email": serialize_email(email),
        "attachments": [serialize_attachment(attachment, email) for attachment in attachments],
        "parse_results": [serialize_parse_result(parse_result) for parse_result in parse_results],
    }


async def _find_thread_for_email(
    session: AsyncSession,
    *,
    message_id: str,
    in_reply_to: str | None,
    references_header: str | None,
    normalized_subject: str | None,
    from_domain: str | None,

) -> EmailThread:
    async def active_thread_for(parent: Email) -> EmailThread | None:
        if not parent.thread_id:
            return None
        candidate = await session.get(EmailThread, parent.thread_id)
        return candidate

    if references_header:
        for reference_id in reversed(re.findall(r"<[^<>]+>", references_header)):
            parent = await session.scalar(select(Email).where(Email.message_id == reference_id))
            if parent:
                thread = await active_thread_for(parent)
                if thread:
                    thread.merge_confidence = 1.0000
                    thread.merge_reason = "References exactly matched an email in an active thread."
                    return thread
    if in_reply_to:
        parent = await session.scalar(select(Email).where(Email.message_id == in_reply_to))
        if parent:
            thread = await active_thread_for(parent)
            if thread:
                thread.merge_confidence = 1.0000
                thread.merge_reason = "In-Reply-To exactly matched an email in an active thread."
                return thread
    create_reason = "Created a new thread because no exact RFC Message-ID relationship matched."
    thread = EmailThread(
        thread_key=sha256_text(f"{message_id}:{normalized_subject or ''}")[:128],
        normalized_subject=normalized_subject,
        root_message_id=message_id,
        thread_version=0,
        email_count=0,
        merge_confidence=1.0000,
        merge_reason=create_reason,
    )
    session.add(thread)
    await session.flush()
    return thread


async def ingest_email(
    session: AsyncSession,
    *,
    payload: EmailIngestRequest,
    user_id: int | None = None,
    auto_parse: bool = True,
    rule_analysis: RuleAnalysisResult | None = None,
) -> dict[str, Any]:
    message_id = normalize_message_id(payload.message_id, fallback_hash=payload.raw_eml_sha256)
    duplicate_predicates = [Email.message_id == message_id]
    if payload.raw_eml_sha256:
        duplicate_predicates.append(Email.source_content_sha256 == payload.raw_eml_sha256)
    duplicate = await session.scalar(select(Email).where(or_(*duplicate_predicates)))
    if duplicate is not None:
        return {"duplicate": True, "email": serialize_email(duplicate)}
    if payload.raw_eml_oss_object_id is None or any(
        attachment.get("oss_object_id") is None for attachment in payload.attachments
    ):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="OSS_ARCHIVAL_REQUIRED")

    normalized_subject = normalize_subject(payload.subject)
    from_domain = address_domain(payload.from_address)
    processing_trace_id = get_correlation_id() or uuid4().hex
    thread = await _find_thread_for_email(
        session,
        message_id=message_id,
        in_reply_to=payload.in_reply_to,
        references_header=payload.references_header,
        normalized_subject=normalized_subject,
        from_domain=from_domain,
    )
    created_at = utcnow()
    email = Email(
        thread_id=thread.id,
        mail_direction="inbound",
        mailbox_account=payload.mailbox_account,
        folder_name=payload.folder_name,
        imap_uid=payload.imap_uid,
        fetch_job_run_id=payload.fetch_job_run_id,
        message_id=message_id,
        raw_eml_oss_object_id=payload.raw_eml_oss_object_id,
        processing_trace_id=processing_trace_id,
        source_content_sha256=payload.raw_eml_sha256,
        raw_headers={
            key: value
            for key, value in {
                "raw_eml_sha256": payload.raw_eml_sha256,
                "delivered_to": payload.delivered_to_addresses,
                "x_original_to": payload.x_original_to_addresses,
            }.items()
            if value
        }
        or None,
        in_reply_to=payload.in_reply_to,
        references_header=payload.references_header,
        from_address=payload.from_address,
        from_domain=from_domain,
        to_addresses=payload.to_addresses,
        cc_addresses=payload.cc_addresses,
        subject=payload.subject,
        normalized_subject=normalized_subject,

        sent_at=payload.sent_at,
        received_at=payload.received_at or utcnow(),
        text_body=payload.text_body,
        html_body=payload.html_body,
        clean_body=normalize_email_body(payload.text_body or html_to_text(payload.html_body)),
        latest_reply_segment=extract_latest_reply_segment(payload.text_body or html_to_text(payload.html_body)),
        parse_status="pending",
        processing_stage="classified",
        created_at=created_at,
        updated_at=created_at,
    )
    analysis = rule_analysis or analyze_email_rules(email)
    email.intent_type = analysis.intent_type
    email.intent_subtype = analysis.intent_subtype
    email.handling_level = analysis.handling_level
    email.classification_version = analysis.classification_version
    email.classification_confidence = analysis.classification_confidence
    email.classification_reason_code = analysis.classification_reason_code
    if analysis.intent_type == "irrelevant":
        email.parse_status = "skipped"
        email.processing_stage = "completed"
        email.terminal_reason_code = "IRRELEVANT_EMAIL"
        email.retryable = False
        email.recovery_stage = None
    session.add(email)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="EMAIL_DUPLICATE") from exc
    thread.latest_email_id = email.id
    thread.email_count = (thread.email_count or 0) + 1
    thread.thread_version = int(thread.thread_version or 0) + 1

    for attachment_payload in payload.attachments:
        session.add(
            EmailAttachment(
                email_id=email.id,
                oss_object_id=attachment_payload.get("oss_object_id"),
                file_name=attachment_payload.get("file_name") or "attachment",
                content_type=attachment_payload.get("content_type"),
                file_size=attachment_payload.get("file_size"),
                file_hash=attachment_payload.get("file_hash"),
                is_inline=attachment_payload.get("is_inline", False),
                content_id=attachment_payload.get("content_id"),
                parse_status=attachment_payload.get("parse_status", "pending"),
                extracted_text=attachment_payload.get("extracted_text"),
                extracted_json=attachment_payload.get("extracted_json"),
                parse_error=attachment_payload.get("parse_error"),
            )
        )
    parse_payload = analysis.to_parse_payload()
    rule_parse = ParseResult(
        email_id=email.id,
        ticket_id=None,
        parser_type="rule",
        parser_version="pre-archive-v2",
        intent_type=parse_payload["intent_type"],
        intent_subtype=parse_payload["intent_subtype"],
        handling_level=parse_payload.get("handling_level"),
        classification_version=parse_payload.get("classification_version"),
        classification_confidence=parse_payload.get("classification_confidence"),
        classification_reason_code=parse_payload.get("classification_reason_code"),
        extracted_fields=parse_payload["extracted_fields"],
        extracted_items=parse_payload["extracted_items"],
        missing_fields=parse_payload["missing_fields"],
        conflict_fields=parse_payload["conflict_fields"],
        confidence_score=parse_payload["confidence_score"],
        field_confidences=parse_payload["field_confidences"],
        evidence=parse_payload["evidence"],
        apply_status="candidate_only",
    )
    session.add(rule_parse)
    await session.flush()
    await log_operation(
        session,
        user_id=user_id,
        operation_type="email_ingested",
        target_type="email",
        target_id=email.id,
        correlation_id=processing_trace_id,
        email_id=email.id,
        description=analysis.classification_reason,
        after_data={
            "intent_type": analysis.intent_type,
            "classification_confidence": analysis.classification_confidence,
            "rule_parse_result_id": rule_parse.id,
            "thread_id": thread.id,
            "thread_reason": thread.merge_reason,
            "in_reply_to": payload.in_reply_to,
            "references_header": payload.references_header,
        },
    )
    await log_system_event(
        session,
        event_type="email_processing",
        module_name="emails",
        correlation_id=processing_trace_id,
        email_id=email.id,
        event_stage="formal_ingest",
        event_status="success",

        target_type="email",
        target_id=email.id,
        message="Email and attachment metadata created after complete archival",
        details={
            "attachment_count": len(payload.attachments),
            "intent_type": analysis.intent_type,
            "rule_parse_result_id": rule_parse.id,
        },
    )
    result: dict[str, Any] = {
        "duplicate": False,
        "email": serialize_email(email),
        "rule_parse_result_id": rule_parse.id,
        "classification": {
            "confidence": analysis.classification_confidence,
            "reason": analysis.classification_reason,
        },
    }
    if auto_parse:
        result["parse"] = await dispatch_email_parse(
            session,
            email_id=email.id,
            user_id=user_id,
            reason="复用归档前规则解析结果并提交 AI 判断。",
            rule_parse_result_id=rule_parse.id,
        )
    return result


def _parse_requires_manual(
    parse_result: ParseResult | None,
    attachments: list[EmailAttachment] | None = None,
) -> bool:
    if parse_result is None:
        return True
    confidence = float(parse_result.confidence_score or 0)
    if (
        parse_result.intent_type in {None, "", "unknown"}
        or confidence < settings.AUTO_APPLY_MIN_CONFIDENCE
        or bool(parse_result.conflict_fields)
    ):
        return True
    for attachment in attachments or []:
        if attachment.parse_status not in {"needs_manual_review", "unsupported", "failed"}:
            continue
        if attachment_type(attachment) == "prc" and not parse_result.missing_fields:
            continue
        return True
    return False


def _manual_reason(
    parse_result: ParseResult | None,
    attachments: list[EmailAttachment] | None = None,
) -> str:
    if parse_result is None:
        return "AI 未返回有效解析结果，需要人工根据原始邮件确认。"
    evidence = parse_result.evidence or {}
    direction = evidence.get("manual_review_direction")
    if isinstance(direction, str) and direction.strip():
        return direction.strip()
    reasons: list[str] = []
    if parse_result.intent_type in {None, "", "unknown"}:
        reasons.append("邮件类型不明确")
    if parse_result.confidence_score is None or float(parse_result.confidence_score) < settings.AUTO_APPLY_MIN_CONFIDENCE:
        reasons.append(f"AI 置信度低于自动采纳阈值 {settings.AUTO_APPLY_MIN_CONFIDENCE}")
    if parse_result.missing_fields:
        reasons.append("存在缺失字段：" + "、".join(sorted(parse_result.missing_fields.keys())))
    if parse_result.conflict_fields:
        reasons.append("存在冲突或异常字段：" + "、".join(sorted(parse_result.conflict_fields.keys())))
    attachment_errors = [
        f"{attachment.file_name}:{attachment.parse_error or attachment.parse_status}"
        for attachment in attachments or []
        if attachment.parse_status in {"needs_manual_review", "unsupported", "failed"}
    ]
    if attachment_errors:
        reasons.append("附件需要人工复核：" + "；".join(attachment_errors[:10]))
    return "；".join(reasons) or "需要人工复核 AI 解析结果。"


ORPHAN_REVIEW_RULES: dict[str, tuple[str, str]] = {
    "customer_supplement": (
        "customer_supplement_orphaned",
        "补充邮件缺少可精确关联的 In-Reply-To/References，仅创建独立人工核对工单，不关联既有工单。",
    ),
    "normal_reply": (
        "normal_reply_orphaned",
        "普通回复缺少可精确关联的 In-Reply-To/References，仅创建独立人工核对工单，不创建空白已解析工单。",
    ),
    "rma_sent": (
        "rma_sent_orphaned",
        "RMA 状态邮件缺少可精确关联的 In-Reply-To/References，仅创建独立人工核对工单。",
    ),
    "device_received": (
        "device_received_orphaned",
        "收货通知缺少可精确关联的 In-Reply-To/References，仅创建独立人工核对工单，不修改任何已有工单。",
    ),
}
POST_CLOSE_TERMINAL_INTENTS = {
    "normal_reply": "POST_RMA_THREAD_REPLY_ARCHIVED",

    "rma_sent": "POST_RMA_STATUS_ARCHIVED",
    "device_received": "POST_RMA_DEVICE_EVENT_ARCHIVED",
}


async def _link_post_close_email(
    session: AsyncSession,
    *,
    email: Email,
    ticket: RepairTicket,
    link_type: str,
    reason: str,
) -> None:
    existing = await session.scalar(
        select(EmailTicketLink).where(
            EmailTicketLink.email_id == email.id,
            EmailTicketLink.ticket_id == ticket.id,
            EmailTicketLink.link_type == link_type,
        )
    )
    if existing is None:
        session.add(
            EmailTicketLink(
                email_id=email.id,
                ticket_id=ticket.id,
                link_type=link_type,
                link_reason=reason,
            )
        )


async def _create_orphan_review_ticket(
    session: AsyncSession,
    *,
    email: Email,
    parse_result: ParseResult,
) -> tuple[RepairTicket, str] | None:
    rule = ORPHAN_REVIEW_RULES.get(parse_result.intent_type or "")
    if rule is None:
        return None
    task_type, reason = rule
    ticket = await ensure_manual_review_ticket_from_parse_result(
        session,
        email=email,
        parse_result=parse_result,
        reason=reason,
        task_type=task_type,
    )
    parse_result.apply_status = "needs_manual_review"
    parse_result.accepted = False
    email.parse_status = "needs_manual"
    return ticket, reason


async def _try_create_reply_draft(
    session: AsyncSession,
    *,
    ticket_id: int | None,
    user_id: int | None,
    email_id: int,
    parse_result: ParseResult | None,
) -> dict[str, Any] | None:
    if ticket_id is None:
        return None
    ticket = await session.get(RepairTicket, ticket_id)
    if ticket is None:
        return None
    if ticket.current_status_code == "manual_review" or ticket.sn_validation_status == "failed":
        return {"created": False, "error_code": "MANUAL_REVIEW_BLOCKS_AUTO_REPLY"}
    effective_missing_fields = ticket.missing_fields if ticket is not None else (parse_result.missing_fields if parse_result else None)
    if parse_result and parse_result.conflict_fields and "sn" in parse_result.conflict_fields:
        return {"created": False, "error_code": "FIELD_CONFLICT_BLOCKS_AUTO_REPLY"}
    elif effective_missing_fields:
        if ticket.current_status_code != "need_customer_info":
            return {"created": False, "error_code": "FOLLOWUP_TICKET_NOT_WAITING_CUSTOMER_INFO"}
        reply_type = "missing_fields"
    else:
        reply_type = "receipt"
    try:
        return await create_reply_draft(
            session,
            ticket_id=ticket_id,
            user_id=user_id,
            reply_type=reply_type,
            related_email_id=email_id,
            missing_fields=effective_missing_fields,
        )
    except HTTPException as exc:
        error_code = str(exc.detail) if isinstance(exc.detail, str) else "REPLY_DRAFT_FAILED"
        await log_system_event(
            session,
            event_type="reply_draft_generation",
            module_name="emails",
            email_id=email_id,
            ticket_id=ticket_id,
            event_stage="reply_draft",
            event_status="failed",
            target_type="ticket",
            target_id=ticket_id,
            error_code=error_code,

            severity="error",
            message="Reply draft generation failed without blocking email parsing",
        )
        return {"created": False, "error_code": error_code}


async def adopt_email_parse_candidate(
    session: AsyncSession,
    *,
    email: Email,
    rule_parse: ParseResult,
    ai_parse: ParseResult | None,
    attachments: list[EmailAttachment],
    thread: EmailThread | None,
    thread_ticket: RepairTicket | None,
    predecessor_ticket: RepairTicket | None,
    user_id: int | None,
    reason: str | None,
    orchestrate_downstream: bool,
) -> dict[str, Any]:
    """Apply a persisted parse candidate with existing deterministic business rules."""
    closed_predecessor = bool(
        predecessor_ticket and predecessor_ticket.current_status_code == "closed"
    )
    ai_applied: dict[str, Any] | None = None
    manual_ticket: dict[str, Any] | None = None
    draft_result: dict[str, Any] | None = None

    if isinstance(ai_parse, ParseResult):
        resolved_intent, resolved_reason = _contextual_intent(email, ai_parse.intent_type, thread_ticket)
        resolved_intent = _set_classification(
            email,
            ai_parse,
            intent_type=resolved_intent,
            confidence=float(ai_parse.confidence_score or 0),
            reason_code=resolved_reason,
        )
        if (
            resolved_intent == EmailIntent.THREAD_NEW_REPAIR
            and thread is not None
            and thread_ticket is not None
            and thread_ticket.current_status_code == "closed"
        ):
            thread.ticket_id = None
            thread_ticket = None
        if resolved_intent in LIFECYCLE_INTENTS:
            ai_parse.apply_status = "auto_skipped"
            email.parse_status = "skipped"
            email.processing_stage = "completed"
            email.terminal_reason_code = f"LIFECYCLE_ONLY_{resolved_intent.upper()}"
            email.retryable = False
            await _link_lifecycle_email(session, email=email, ticket=thread_ticket, intent_type=resolved_intent)
        elif resolved_intent in MANUAL_INTENTS:
            reason_text = f"已识别为 SECOND/{resolved_intent}，当前自动 RMA 系统不执行该业务。"
            active_first = bool(
                thread_ticket
                and thread_ticket.ticket_category == "standard_repair"
                and thread_ticket.current_status_code not in {"closed", "resolved"}
            )
            if active_first:
                sidecar = await create_manual_business_ticket_from_email(
                    session, email=email, parse_result=ai_parse, reason=reason_text
                )
                manual_ticket = {"ticket_id": sidecar.id, "ticket_no": sidecar.ticket_no, "reason": reason_text}
            else:
                if thread_ticket is not None:
                    await _link_post_close_email(
                        session,
                        email=email,
                        ticket=thread_ticket,
                        link_type="manual_business_context",

                        reason=reason_text,
                    )
                task = await create_email_manual_task_if_missing(
                    session,
                    email=email,
                    task_type=f"second_{resolved_intent}",
                    trigger_reason=reason_text,
                    recovery_action="人工处理、重分类、关联/创建工单或记录外部处理结果。",
                )
                manual_ticket = {"ticket_id": None, "task_id": task.id, "reason": reason_text}
            ai_parse.apply_status = "needs_manual_review"
            email.parse_status = "needs_manual"
            email.processing_stage = "manual_review"
            email.retryable = False
        elif resolved_intent == EmailIntent.UNKNOWN:
            task = await create_email_manual_task_if_missing(
                session,
                email=email,
                task_type="unknown_email_classification",
                trigger_reason=_manual_reason(ai_parse, list(attachments)),
                priority="high",
                recovery_action="查看正文、附件、回复链和候选工单后人工定类或重新解析。",
            )
            ai_parse.apply_status = "needs_manual_review"
            email.parse_status = "needs_manual"
            email.processing_stage = "manual_review"
            email.retryable = True
            email.recovery_stage = "email_classification"
            manual_ticket = {"ticket_id": None, "task_id": task.id, "reason": _manual_reason(ai_parse, list(attachments))}
        elif (
            closed_predecessor
            and predecessor_ticket is not None
            and ai_parse.intent_type in POST_CLOSE_TERMINAL_INTENTS
            and not _parse_requires_manual(ai_parse, list(attachments))
        ):
            terminal_reason = POST_CLOSE_TERMINAL_INTENTS[ai_parse.intent_type]
            ai_parse.apply_status = "auto_skipped"
            email.parse_status = "skipped"
            email.terminal_reason_code = terminal_reason
            await _link_post_close_email(
                session,
                email=email,
                ticket=predecessor_ticket,
                link_type="post_close_event",
                reason=terminal_reason,
            )
        elif ai_parse.intent_type == "irrelevant" and not _parse_requires_manual(ai_parse, list(attachments)):
            ai_parse.apply_status = "auto_skipped"
            email.parse_status = "skipped"
            email.terminal_reason_code = (
                "OUT_OF_SCOPE_REPAIR"
                if ai_parse.intent_subtype == "out_of_scope_repair"
                else "IRRELEVANT_EMAIL"
            )
            if closed_predecessor and predecessor_ticket is not None:
                await _link_post_close_email(
                    session,
                    email=email,
                    ticket=predecessor_ticket,
                    link_type="post_close_irrelevant",
                    reason=email.terminal_reason_code,
                )
        elif (
            closed_predecessor
            and predecessor_ticket is not None
            and ai_parse.intent_type != "new_repair"
        ):
            closed_reason = (
                "已关闭工单线程的新邮件已完成分类，但内容可能要求修改既有RMA"
                "或分类置信度不足，需要在不重开旧工单的前提下人工处理。"
            )
            await _link_post_close_email(
                session,
                email=email,
                ticket=predecessor_ticket,
                link_type="post_close_manual",
                reason=closed_reason,
            )
            await create_manual_task_if_missing(
                session,
                ticket=predecessor_ticket,
                task_type="post_rma_email_review",
                trigger_reason=closed_reason,
                priority="high",
                email_id=email.id,
            )
            ai_parse.apply_status = "needs_manual_review"
            email.parse_status = "needs_manual"
            manual_ticket = {
                "ticket_id": predecessor_ticket.id,
                "ticket_no": predecessor_ticket.ticket_no,
                "reason": closed_reason,
            }
        elif thread_ticket is None and ai_parse.intent_type in ORPHAN_REVIEW_RULES:
            orphan_review = await _create_orphan_review_ticket(session, email=email, parse_result=ai_parse)
            if orphan_review is not None:
                ticket, orphan_reason = orphan_review
                manual_ticket = {"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "reason": orphan_reason}
        elif _parse_requires_manual(ai_parse, list(attachments)):
            ticket = await ensure_manual_review_ticket_from_parse_result(

                session,
                email=email,
                parse_result=ai_parse,
                reason=_manual_reason(ai_parse, list(attachments)),
                task_type="ai_review_required",
            )
            email.parse_status = "needs_manual"
            manual_ticket = {
                "ticket_id": ticket.id,
                "ticket_no": ticket.ticket_no,
                "reason": _manual_reason(ai_parse, list(attachments)),
            }
            if user_id is not None and ticket.current_status_code == "manual_review":
                ai_applied = await apply_parse_result(
                    session,
                    parse_result_id=ai_parse.id,
                    user_id=user_id,
                    reason=reason or "Manual-review reparse refreshed ticket fields and items.",
                    apply_status="needs_manual_review",
                )
                email.parse_status = "needs_manual"
            draft_result = await _try_create_reply_draft(
                session,
                ticket_id=ticket.id,
                user_id=user_id,
                email_id=email.id,
                parse_result=ai_parse,
            )
        else:
            ai_applied = await apply_parse_result(
                session,
                parse_result_id=ai_parse.id,
                user_id=user_id,
                reason=reason or "AI 结构化解析结果自动采纳。",
                apply_status="auto_applied",
            )
            email.parse_status = "parsed"
            ticket_id = (
                ai_applied.get("ticket", {}).get("id")
                if isinstance(ai_applied, dict) and isinstance(ai_applied.get("ticket"), dict)
                else ai_parse.ticket_id
            )
            if ticket_id is not None:
                applied_ticket = await session.get(RepairTicket, ticket_id)
                if not orchestrate_downstream:
                    # Graph owns validation, follow-up/receipt preparation and
                    # all downstream side-effect orchestration in active mode.
                    pass
                elif (
                    applied_ticket
                    and applied_ticket.current_status_code == "need_customer_info"
                    and bool(ai_parse.missing_fields)
                ):
                    draft_result = await _try_create_reply_draft(
                        session,
                        ticket_id=ticket_id,
                        user_id=user_id,
                        email_id=email.id,
                        parse_result=ai_parse,
                    )
                elif (
                    ai_parse.intent_type in AUTO_INTENTS
                    and applied_ticket is not None
                    and applied_ticket.current_status_code == "manual_review"
                ):
                    ai_applied = {
                        **ai_applied,
                        "export_validation": {
                            "status": "awaiting_manual_resolution",
                            "reason": "MANUAL_REVIEW_REPARSE_REQUIRES_EXPLICIT_RESOLUTION",
                        },
                    }
                elif ai_parse.intent_type in AUTO_INTENTS:
                    validated = await validate_and_mark_ready_for_export(
                        session,
                        ticket_id=ticket_id,
                        user_id=None,
                        enqueue_relay_job=orchestrate_downstream,
                    )
                    ai_applied = {**ai_applied, "export_validation": validated}
                    if validated.get("status") != "ready_for_export":
                        validation_reason = "SN 核心校验或完整安全校验未通过，AI 结果已保留但不得进入可导出状态。"
                        ticket = await ensure_manual_review_ticket_from_parse_result(
                            session,
                            email=email,
                            parse_result=ai_parse,
                            reason=validation_reason,
                            task_type="sn_validation_required",
                        )
                        email.parse_status = "needs_manual"
                        manual_ticket = {"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "reason": validation_reason}
                        draft_result = await _try_create_reply_draft(
                            session,
                            ticket_id=ticket.id,
                            user_id=user_id,
                            email_id=email.id,
                            parse_result=ai_parse,
                        )
                    else:
                        ready_ticket = await session.get(RepairTicket, ticket_id)

                        if not ready_ticket or not ready_ticket.rma_required:
                            draft_result = await _try_create_reply_draft(
                                session,
                                ticket_id=ticket_id,
                                user_id=user_id,
                                email_id=email.id,
                                parse_result=ai_parse,
                            )
    elif normalize_intent(rule_parse.intent_type) in LIFECYCLE_INTENTS:
        resolved_intent = _set_classification(
            email,
            rule_parse,
            intent_type=rule_parse.intent_type,
            confidence=float(rule_parse.confidence_score or 0),
            reason_code="RULE_LIFECYCLE_CLASSIFIED",
        )
        rule_parse.apply_status = "auto_skipped"
        email.parse_status = "skipped"
        email.terminal_reason_code = f"LIFECYCLE_ONLY_{resolved_intent.upper()}"
        await _link_lifecycle_email(session, email=email, ticket=thread_ticket, intent_type=resolved_intent)
    elif normalize_intent(rule_parse.intent_type) in MANUAL_INTENTS:
        resolved_intent, resolved_reason = _contextual_intent(email, rule_parse.intent_type, thread_ticket)
        _set_classification(
            email,
            rule_parse,
            intent_type=resolved_intent,
            confidence=float(rule_parse.confidence_score or 0),
            reason_code=resolved_reason,
        )
        reason_text = f"规则已明确识别为 SECOND/{resolved_intent}；AI 不可用，交人工业务处理。"
        active_first = bool(
            thread_ticket
            and thread_ticket.ticket_category == "standard_repair"
            and thread_ticket.current_status_code not in {"closed", "resolved"}
        )
        if active_first:
            sidecar = await create_manual_business_ticket_from_email(
                session, email=email, parse_result=rule_parse, reason=reason_text
            )
            manual_ticket = {"ticket_id": sidecar.id, "ticket_no": sidecar.ticket_no, "reason": reason_text}
        else:
            task = await create_email_manual_task_if_missing(
                session,
                email=email,
                task_type=f"second_{resolved_intent}",
                trigger_reason=reason_text,
            )
            manual_ticket = {"ticket_id": None, "task_id": task.id, "reason": reason_text}
        rule_parse.apply_status = "needs_manual_review"
        email.parse_status = "needs_manual"
    elif normalize_intent(rule_parse.intent_type) == EmailIntent.UNKNOWN:
        _set_classification(
            email,
            rule_parse,
            intent_type=EmailIntent.UNKNOWN,
            confidence=float(rule_parse.confidence_score or 0),
            reason_code="RULE_UNKNOWN_AI_UNAVAILABLE",
        )
        task = await create_email_manual_task_if_missing(
            session,
            email=email,
            task_type="unknown_email_classification",
            trigger_reason="规则无法可靠识别邮件且 AI 不可用。",
            priority="high",
        )
        rule_parse.apply_status = "needs_manual_review"
        email.parse_status = "needs_manual"
        manual_ticket = {"ticket_id": None, "task_id": task.id, "reason": "规则无法可靠识别邮件且 AI 不可用。"}
    elif (
        closed_predecessor
        and predecessor_ticket is not None
        and rule_parse.intent_type in POST_CLOSE_TERMINAL_INTENTS
        and not _parse_requires_manual(rule_parse, list(attachments))
    ):
        terminal_reason = POST_CLOSE_TERMINAL_INTENTS[rule_parse.intent_type]
        rule_parse.apply_status = "auto_skipped"
        email.parse_status = "skipped"
        email.terminal_reason_code = terminal_reason
        await _link_post_close_email(
            session,
            email=email,
            ticket=predecessor_ticket,
            link_type="post_close_event",
            reason=terminal_reason,
        )
    elif (
        closed_predecessor
        and predecessor_ticket is not None
        and rule_parse.intent_type != "new_repair"
    ):
        closed_reason = "已关闭工单线程的新邮件无法由规则结果安全自动处理，需要人工复核分类及恢复动作。"
        await _link_post_close_email(
            session,
            email=email,
            ticket=predecessor_ticket,
            link_type="post_close_manual",
            reason=closed_reason,
        )
        await create_manual_task_if_missing(
            session,

            ticket=predecessor_ticket,
            task_type="post_rma_email_review",
            trigger_reason=closed_reason,
            priority="high",
            email_id=email.id,
        )
        rule_parse.apply_status = "needs_manual_review"
        email.parse_status = "needs_manual"
        manual_ticket = {
            "ticket_id": predecessor_ticket.id,
            "ticket_no": predecessor_ticket.ticket_no,
            "reason": closed_reason,
        }
    elif thread_ticket is None and rule_parse.intent_type in ORPHAN_REVIEW_RULES:
        orphan_review = await _create_orphan_review_ticket(session, email=email, parse_result=rule_parse)
        if orphan_review is not None:
            ticket, orphan_reason = orphan_review
            manual_ticket = {"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "reason": orphan_reason}
    else:
        ticket = await ensure_manual_review_ticket_from_parse_result(
            session,
            email=email,
            parse_result=rule_parse,
            reason="AI 未配置或未返回有效结果，规则解析仅作为候选，需要人工复核。",
            task_type="ai_unavailable",
        )
        email.parse_status = "needs_manual"
        manual_ticket = {"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "reason": "AI 未配置或未返回有效结果。"}
        draft_result = await _try_create_reply_draft(
            session,
            ticket_id=ticket.id,
            user_id=user_id,
            email_id=email.id,
            parse_result=rule_parse,
        )

    return {
        "ai_applied": ai_applied,
        "manual_ticket": manual_ticket,
        "draft_result": draft_result,
        "selected_parse_result": ai_parse or rule_parse,
    }


async def prepare_email_parse_context(
    session: AsyncSession,
    *,
    email_id: int,
    user_id: int | None = None,
    reason: str | None = None,
    durable_attachment_stages: bool = False,
    rule_parse_result_id: int | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """Prepare deterministic parsing facts without selecting a business route."""
    email = await session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")
    conversation_body = normalize_email_body(email.text_body or html_to_text(email.html_body) or email.clean_body)
    email.clean_body = conversation_body
    email.latest_reply_segment = extract_latest_reply_segment(conversation_body)
    rule_parse = await session.get(ParseResult, rule_parse_result_id) if rule_parse_result_id else None
    if rule_parse is None and execution_id:
        candidates = list(
            (
                await session.execute(
                    select(ParseResult)
                    .where(ParseResult.email_id == email.id, ParseResult.parser_type == "rule")
                    .order_by(ParseResult.id.desc())
                    .limit(20)
                )
            ).scalars().all()
        )
        rule_parse = next(
            (item for item in candidates if (item.evidence or {}).get("graph_execution_id") == execution_id),
            None,
        )
    if rule_parse is not None and (rule_parse.email_id != email.id or rule_parse.parser_type != "rule"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="RULE_PARSE_RESULT_MISMATCH")
    if rule_parse is None:
        analysis = analyze_email_rules(email)
        email.latest_reply_segment = analysis.body
        email.intent_type = analysis.intent_type
        email.intent_subtype = analysis.intent_subtype
        email.handling_level = analysis.handling_level
        email.classification_version = analysis.classification_version
        email.classification_confidence = analysis.classification_confidence
        email.classification_reason_code = analysis.classification_reason_code
        parse_payload = analysis.to_parse_payload()
        rule_parse = ParseResult(
            email_id=email.id,
            ticket_id=None,
            parser_type="rule",
            parser_version="pre-archive-v2",
            intent_type=parse_payload["intent_type"],
            intent_subtype=parse_payload["intent_subtype"],
            handling_level=parse_payload.get("handling_level"),
            classification_version=parse_payload.get("classification_version"),
            classification_confidence=parse_payload.get("classification_confidence"),
            classification_reason_code=parse_payload.get("classification_reason_code"),
            extracted_fields=parse_payload["extracted_fields"],
            extracted_items=parse_payload["extracted_items"],
            missing_fields=parse_payload["missing_fields"],
            conflict_fields=parse_payload["conflict_fields"],
            confidence_score=parse_payload["confidence_score"],
            field_confidences=parse_payload["field_confidences"],
            evidence={
                **parse_payload["evidence"],
                "mode": "explicit_reparse",
                "graph_execution_id": execution_id,
            },
            apply_status="candidate_only",
        )
        session.add(rule_parse)
        await session.flush()
    else:
        email.intent_type = rule_parse.intent_type
        email.intent_subtype = rule_parse.intent_subtype
        email.handling_level = rule_parse.handling_level
        email.classification_version = rule_parse.classification_version
        email.classification_confidence = rule_parse.classification_confidence
        email.classification_reason_code = rule_parse.classification_reason_code
    email.parse_status = "parsing"
    email.processing_stage = "parsing"
    email.terminal_reason_code = None
    email.last_error_code = None
    await log_system_event(
        session,
        event_type="email_processing",
        module_name="emails",
        correlation_id=email.processing_trace_id,
        email_id=email.id,
        event_stage="parse",
        event_status="running",
        target_type="email",
        target_id=email.id,
        message="Email parsing started",
    )
    await log_operation(
        session,
        user_id=user_id,
        operation_type="email_reparsed",
        target_type="email",
        target_id=email.id,
        description=reason,
        after_data={
            "parse_result_id": rule_parse.id,
            "mode": "reuse_pre_archive_rule" if rule_parse_result_id else "explicit_reparse",
        },
    )
    attachments = (
        await session.execute(
            select(EmailAttachment)
            .where(EmailAttachment.email_id == email.id)
            .order_by(EmailAttachment.created_at.desc())
        )
    ).scalars().all()
    multimodal_results: list[dict[str, Any]] = []
    for attachment in attachments:
        if attachment.parse_status in {"parsed", "skipped", "skipped_decorative", "unsupported"}:
            attachment_result = attachment.extracted_json
        else:
            attachment_result = await parse_attachment(session, attachment)
        if attachment_result and attachment.parse_status != "skipped":
            multimodal_results.append(attachment_result)
        if durable_attachment_stages:
            await session.commit()
    if durable_attachment_stages:
        await session.refresh(email)
        await session.refresh(rule_parse)
        for attachment in attachments:
            await session.refresh(attachment)
    thread = await session.get(EmailThread, email.thread_id) if email.thread_id else None
    thread_ticket = await session.get(RepairTicket, thread.ticket_id) if thread and thread.ticket_id else None
    predecessor_ticket = (
        await session.get(RepairTicket, thread.predecessor_ticket_id)
        if thread and thread.predecessor_ticket_id
        else None
    )
    return {
        "email_id": email.id,
        "rule_parse_result_id": rule_parse.id,
        "attachment_ids": [item.id for item in attachments],
        "thread_id": thread.id if thread else None,
        "thread_ticket_id": thread_ticket.id if thread_ticket else None,
        "predecessor_ticket_id": predecessor_ticket.id if predecessor_ticket else None,
        "execution_id": execution_id,
    }


async def generate_email_ai_candidate(
    session: AsyncSession,
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Generate and persist one AI candidate; no candidate adoption occurs here."""
    email = await session.get(Email, int(context["email_id"]))
    rule_parse = await session.get(ParseResult, int(context["rule_parse_result_id"]))
    if email is None or rule_parse is None:
        raise LookupError("EMAIL_PARSE_CONTEXT_NOT_FOUND")
    execution_id = context.get("execution_id")
    if execution_id:
        if (rule_parse.evidence or {}).get("graph_ai_attempt_execution_id") == execution_id:
            return {"ai_parse_result_id": None, "ai_available": False}
        candidates = list(
            (
                await session.execute(
                    select(ParseResult)
                    .where(ParseResult.email_id == email.id, ParseResult.parser_type == "ai")
                    .order_by(ParseResult.id.desc())
                    .limit(20)
                )
            ).scalars().all()
        )
        existing = next(
            (item for item in candidates if (item.evidence or {}).get("graph_execution_id") == execution_id),
            None,
        )
        if existing is not None:
            return {"ai_parse_result_id": existing.id, "ai_available": True}
    attachments = list(
        (
            await session.execute(
                select(EmailAttachment).where(EmailAttachment.id.in_(context.get("attachment_ids") or []))
            )
        ).scalars().all()
    )
    thread = await session.get(EmailThread, context.get("thread_id")) if context.get("thread_id") else None
    thread_ticket = await session.get(RepairTicket, context.get("thread_ticket_id")) if context.get("thread_ticket_id") else None
    result = await create_ai_parse_candidate(
        session,
        email=email,
        attachments=attachments,
        mode="classification_and_extract",
        ticket_id=thread.ticket_id if thread else None,
        rule_context={
            "parse_result_id": rule_parse.id,
            "intent_type": rule_parse.intent_type,
            "confidence_score": float(rule_parse.confidence_score or 0),
            "missing_fields": rule_parse.missing_fields,
            "conflict_fields": rule_parse.conflict_fields,
            "evidence": rule_parse.evidence,
            "thread_context": {
                "thread_id": thread.id if thread else None,
                "has_reply_headers": bool(email.in_reply_to or email.references_header),
                "active_ticket_id": thread_ticket.id if thread_ticket else None,
                "active_ticket_category": thread_ticket.ticket_category if thread_ticket else None,
                "active_ticket_status": thread_ticket.current_status_code if thread_ticket else None,
                "active_ticket_missing_fields": thread_ticket.missing_fields if thread_ticket else None,
                "active_ticket_has_rma": bool(thread_ticket and thread_ticket.rma_status not in {None, "not_required", "pending"}),
            },
        },
        multimodal_results=[
            item.extracted_json
            for item in attachments
            if item.extracted_json and item.parse_status != "skipped"
        ] or None,
    )
    ai_parse = result.get("parse_result") if result else None
    if execution_id:
        rule_parse.evidence = {
            **(rule_parse.evidence or {}),
            "graph_ai_attempt_execution_id": execution_id,
        }
    if isinstance(ai_parse, ParseResult) and execution_id:
        ai_parse.evidence = {**(ai_parse.evidence or {}), "graph_execution_id": execution_id}
    return {
        "ai_parse_result_id": ai_parse.id if isinstance(ai_parse, ParseResult) else None,
        "ai_available": isinstance(ai_parse, ParseResult),
    }


async def adopt_email_parse_context(
    session: AsyncSession,
    *,
    context: dict[str, Any],
    ai_candidate: dict[str, Any],
    user_id: int | None = None,
    reason: str | None = None,
    orchestrate_downstream: bool = False,
) -> dict[str, Any]:
    """Load persisted facts and apply the extracted deterministic adoption service."""
    email = await session.get(Email, int(context["email_id"]))
    rule_parse = await session.get(ParseResult, int(context["rule_parse_result_id"]))
    ai_parse = (
        await session.get(ParseResult, int(ai_candidate["ai_parse_result_id"]))
        if ai_candidate.get("ai_parse_result_id")
        else None
    )
    if email is None or rule_parse is None:
        raise LookupError("EMAIL_PARSE_CONTEXT_NOT_FOUND")
    selected_before = ai_parse or rule_parse
    execution_id = context.get("execution_id")
    adoption_marker = (selected_before.evidence or {}).get("graph_adoption_execution_id")
    attachments = list(
        (
            await session.execute(
                select(EmailAttachment).where(EmailAttachment.id.in_(context.get("attachment_ids") or []))
            )
        ).scalars().all()
    )
    thread = await session.get(EmailThread, context.get("thread_id")) if context.get("thread_id") else None
    thread_ticket = await session.get(RepairTicket, context.get("thread_ticket_id")) if context.get("thread_ticket_id") else None
    predecessor_ticket = (
        await session.get(RepairTicket, context.get("predecessor_ticket_id"))
        if context.get("predecessor_ticket_id")
        else None
    )
    if execution_id and adoption_marker == execution_id:
        result = {
            "ai_applied": None,
            "manual_ticket": None,
            "draft_result": None,
            "selected_parse_result": selected_before,
        }
    else:
        result = await adopt_email_parse_candidate(
            session,
            email=email,
            rule_parse=rule_parse,
            ai_parse=ai_parse,
            attachments=attachments,
            thread=thread,
            thread_ticket=thread_ticket,
            predecessor_ticket=predecessor_ticket,
            user_id=user_id,
            reason=reason,
            orchestrate_downstream=orchestrate_downstream,
        )
    selected = result["selected_parse_result"]
    if execution_id:
        selected.evidence = {
            **(selected.evidence or {}),
            "graph_adoption_execution_id": execution_id,
        }
    selected_ticket = (
        await session.get(RepairTicket, selected.ticket_id)
        if selected.ticket_id is not None
        else None
    )
    manual_task_predicate = ManualReviewTask.email_id == email.id
    if selected.ticket_id is not None:
        manual_task_predicate = or_(
            ManualReviewTask.ticket_id == selected.ticket_id,
            ManualReviewTask.email_id == email.id,
        )
    manual_task = await session.scalar(
        select(ManualReviewTask)
        .where(
            manual_task_predicate,
            ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
        )
        .order_by(ManualReviewTask.id.desc())
    )
    if email.parse_status == "parsed":
        email.processing_stage = "completed"
        email.terminal_reason_code = "EMAIL_PROCESSING_COMPLETED"
        email.retryable = False
        email.recovery_stage = None
        email.next_retry_at = None
    elif email.parse_status == "skipped":
        email.processing_stage = "completed"
        email.terminal_reason_code = email.terminal_reason_code or "EMAIL_PROCESSING_SKIPPED"
        email.retryable = False
        email.recovery_stage = None
        email.next_retry_at = None
    elif email.parse_status == "needs_manual":
        email.processing_stage = "manual_review"
        email.terminal_reason_code = "EMAIL_REQUIRES_MANUAL_REVIEW"
        email.retryable = True
        email.recovery_stage = "manual_review"
    await log_system_event(
        session,
        event_type="email_processing",
        module_name="emails",
        correlation_id=email.processing_trace_id,
        email_id=email.id,
        ticket_id=selected.ticket_id,
        event_stage="parse",
        event_status=email.parse_status,
        target_type="email",
        target_id=email.id,
        message="Email parsing completed",
        details={
            "attachment_count": len(attachments),
            "attachment_manual_count": sum(
                1
                for attachment in attachments
                if attachment.parse_status in {"needs_manual_review", "unsupported", "failed"}
            ),
            "manual_ticket_created": bool(result["manual_ticket"]),
            "draft_created": bool(result["draft_result"]),
            "workflow_engine": "langgraph" if not orchestrate_downstream else "legacy",
        },
    )
    return {
        "email_id": email.id,
        "email_parse_status": email.parse_status,
        "ticket_id": selected.ticket_id,
        "ticket_version": selected_ticket.version if selected_ticket is not None else None,
        "parse_result_id": selected.id,
        "intent_type": selected.intent_type,
        "intent_subtype": selected.intent_subtype,
        "handling_level": selected.handling_level,
        "confidence_score": float(selected.confidence_score or 0),
        "missing_fields": selected.missing_fields or {},
        "conflict_fields": selected.conflict_fields or {},
        "ai_applied": result["ai_applied"],
        "manual_ticket": result["manual_ticket"],
        "manual_task_id": manual_task.id if manual_task is not None else None,
        "draft_result": result["draft_result"],
    }


async def reparse_email(
    session: AsyncSession,
    *,
    email_id: int,
    user_id: int | None = None,
    reason: str | None = None,
    durable_attachment_stages: bool = False,
    rule_parse_result_id: int | None = None,
) -> dict[str, Any]:
    email = await session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")

    conversation_body = normalize_email_body(email.text_body or html_to_text(email.html_body) or email.clean_body)
    email.clean_body = conversation_body
    email.latest_reply_segment = extract_latest_reply_segment(conversation_body)
    rule_parse = await session.get(ParseResult, rule_parse_result_id) if rule_parse_result_id else None
    if rule_parse is not None and (rule_parse.email_id != email.id or rule_parse.parser_type != "rule"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="RULE_PARSE_RESULT_MISMATCH")
    if rule_parse is None:
        analysis = analyze_email_rules(email)
        email.latest_reply_segment = analysis.body
        email.intent_type = analysis.intent_type
        email.intent_subtype = analysis.intent_subtype
        email.handling_level = analysis.handling_level
        email.classification_version = analysis.classification_version
        email.classification_confidence = analysis.classification_confidence
        email.classification_reason_code = analysis.classification_reason_code
        parse_payload = analysis.to_parse_payload()
        rule_parse = ParseResult(
            email_id=email.id,
            ticket_id=None,
            parser_type="rule",
            parser_version="pre-archive-v2",
            intent_type=parse_payload["intent_type"],
            intent_subtype=parse_payload["intent_subtype"],
            handling_level=parse_payload.get("handling_level"),
            classification_version=parse_payload.get("classification_version"),
            classification_confidence=parse_payload.get("classification_confidence"),
            classification_reason_code=parse_payload.get("classification_reason_code"),
            extracted_fields=parse_payload["extracted_fields"],
            extracted_items=parse_payload["extracted_items"],
            missing_fields=parse_payload["missing_fields"],
            conflict_fields=parse_payload["conflict_fields"],
            confidence_score=parse_payload["confidence_score"],
            field_confidences=parse_payload["field_confidences"],
            evidence={**parse_payload["evidence"], "mode": "explicit_reparse"},
            apply_status="candidate_only",
        )
        session.add(rule_parse)
        await session.flush()
    else:
        email.intent_type = rule_parse.intent_type
        email.intent_subtype = rule_parse.intent_subtype
        email.handling_level = rule_parse.handling_level
        email.classification_version = rule_parse.classification_version
        email.classification_confidence = rule_parse.classification_confidence
        email.classification_reason_code = rule_parse.classification_reason_code
    email.parse_status = "parsing"
    email.processing_stage = "parsing"
    email.terminal_reason_code = None
    email.last_error_code = None

    await log_system_event(
        session,
        event_type="email_processing",
        module_name="emails",
        correlation_id=email.processing_trace_id,
        email_id=email.id,
        event_stage="parse",
        event_status="running",
        target_type="email",
        target_id=email.id,
        message="Email parsing started",
    )

    await log_operation(
        session,
        user_id=user_id,
        operation_type="email_reparsed",
        target_type="email",
        target_id=email.id,
        description=reason,
        after_data={
            "parse_result_id": rule_parse.id,
            "mode": "reuse_pre_archive_rule" if rule_parse_result_id else "explicit_reparse",
        },
    )

    attachments = (
        await session.execute(select(EmailAttachment).where(EmailAttachment.email_id == email.id).order_by(EmailAttachment.created_at.desc()))
    ).scalars().all()

    multimodal_results: list[dict[str, Any]] = []

    for attachment in attachments:
        if attachment.parse_status in {"parsed", "skipped", "skipped_decorative", "unsupported"}:
            attachment_result = attachment.extracted_json
        else:
            attachment_result = await parse_attachment(session, attachment)
        if attachment_result and attachment.parse_status != "skipped":
            multimodal_results.append(attachment_result)
        if durable_attachment_stages:
            await session.commit()

    if durable_attachment_stages:
        await session.refresh(email)
        await session.refresh(rule_parse)
        for attachment in attachments:
            await session.refresh(attachment)

    thread = await session.get(EmailThread, email.thread_id) if email.thread_id else None
    thread_ticket = await session.get(RepairTicket, thread.ticket_id) if thread and thread.ticket_id else None
    predecessor_ticket = (
        await session.get(RepairTicket, thread.predecessor_ticket_id)
        if thread and thread.predecessor_ticket_id
        else None
    )
    closed_predecessor = bool(
        predecessor_ticket and predecessor_ticket.current_status_code == "closed"
    )
    ai_result = await create_ai_parse_candidate(
        session,
        email=email,
        attachments=list(attachments),
        mode="classification_and_extract",
        ticket_id=thread.ticket_id if thread else None,
        rule_context={
            "parse_result_id": rule_parse.id,
            "intent_type": rule_parse.intent_type,
            "confidence_score": float(rule_parse.confidence_score or 0),
            "missing_fields": rule_parse.missing_fields,
            "conflict_fields": rule_parse.conflict_fields,
            "evidence": rule_parse.evidence,
            "thread_context": {
                "thread_id": thread.id if thread else None,
                "has_reply_headers": bool(email.in_reply_to or email.references_header),
                "active_ticket_id": thread_ticket.id if thread_ticket else None,
                "active_ticket_category": thread_ticket.ticket_category if thread_ticket else None,
                "active_ticket_status": thread_ticket.current_status_code if thread_ticket else None,
                "active_ticket_missing_fields": thread_ticket.missing_fields if thread_ticket else None,
                "active_ticket_has_rma": bool(thread_ticket and thread_ticket.rma_status not in {None, "not_required", "pending"}),
            },
        },
        multimodal_results=multimodal_results or None,
    )

    ai_parse = ai_result["parse_result"] if ai_result else None
    ai_applied: dict[str, Any] | None = None
    manual_ticket: dict[str, Any] | None = None
    draft_result: dict[str, Any] | None = None

    adoption = await adopt_email_parse_candidate(
        session,
        email=email,
        rule_parse=rule_parse,
        ai_parse=ai_parse if isinstance(ai_parse, ParseResult) else None,
        attachments=list(attachments),
        thread=thread,
        thread_ticket=thread_ticket,
        predecessor_ticket=predecessor_ticket,
        user_id=user_id,
        reason=reason,
        orchestrate_downstream=True,
    )
    ai_applied = adoption["ai_applied"]
    manual_ticket = adoption["manual_ticket"]
    draft_result = adoption["draft_result"]

    if email.parse_status == "parsed":
        email.processing_stage = "completed"
        email.terminal_reason_code = "EMAIL_PROCESSING_COMPLETED"
        email.retryable = False
        email.recovery_stage = None
        email.next_retry_at = None
    elif email.parse_status == "skipped":
        email.processing_stage = "completed"
        email.terminal_reason_code = email.terminal_reason_code or "EMAIL_PROCESSING_SKIPPED"
        email.retryable = False
        email.recovery_stage = None
        email.next_retry_at = None
    elif email.parse_status == "needs_manual":
        email.processing_stage = "manual_review"
        email.terminal_reason_code = "EMAIL_REQUIRES_MANUAL_REVIEW"
        email.retryable = True
        email.recovery_stage = "manual_review"

    await log_system_event(
        session,
        event_type="email_processing",
        module_name="emails",
        correlation_id=email.processing_trace_id,
        email_id=email.id,
        ticket_id=ai_parse.ticket_id if isinstance(ai_parse, ParseResult) else None,
        event_stage="parse",
        event_status=email.parse_status,
        target_type="email",
        target_id=email.id,
        message="Email parsing completed",
        details={
            "attachment_count": len(attachments),
            "attachment_manual_count": sum(
                1 for attachment in attachments if attachment.parse_status in {"needs_manual_review", "unsupported", "failed"}
            ),
            "manual_ticket_created": bool(manual_ticket),
            "draft_created": bool(draft_result and draft_result.get("created", True)),
        },
    )
    legacy_result = {
        "parse_result": serialize_parse_result(rule_parse),
        "applied": None,
        "ai_parse_result": serialize_parse_result(ai_parse) if isinstance(ai_parse, ParseResult) else None,
        "ai_applied": ai_applied,
        "manual_ticket": manual_ticket,
        "draft_result": draft_result,
    }
    if settings.WORKFLOW_ENGINE == "shadow":
        try:
            from app.workflows.email_ticket.runner import run_and_record_shadow_comparison

            await run_and_record_shadow_comparison(
                session,
                email_id=email.id,
                legacy_outcome=legacy_result,
            )
        except Exception as exc:
            await log_system_event(
                session,
                event_type="langgraph_shadow_failed",
                module_name="email_ticket_workflow",
                message="Read-only shadow workflow failed; legacy result remains authoritative",
                correlation_id=email.processing_trace_id,
                email_id=email.id,
                event_stage="shadow_comparison",
                event_status="failed",
                error_code=exc.__class__.__name__,
            )
    return legacy_result


async def dispatch_email_parse(
    session: AsyncSession,
    *,
    email_id: int,
    user_id: int | None = None,
    reason: str | None = None,
    durable_attachment_stages: bool = False,
    rule_parse_result_id: int | None = None,
    workflow_execution_id: str | None = None,
) -> dict[str, Any]:
    """Select exactly one orchestration engine for a persisted email."""
    if not _use_langgraph_for_email(email_id):
        return await reparse_email(
            session,
            email_id=email_id,
            user_id=user_id,
            reason=reason,
            durable_attachment_stages=durable_attachment_stages,
            rule_parse_result_id=rule_parse_result_id,
        )
    return await _dispatch_langgraph_email_parse(
        session,
        email_id=email_id,
        user_id=user_id,
        reason=reason,
        durable_attachment_stages=durable_attachment_stages,
        rule_parse_result_id=rule_parse_result_id,
        workflow_execution_id=workflow_execution_id,
    )


async def _dispatch_langgraph_email_parse(
    session: AsyncSession,
    *,
    email_id: int,
    user_id: int | None = None,
    reason: str | None = None,
    durable_attachment_stages: bool = False,
    rule_parse_result_id: int | None = None,
    workflow_execution_id: str | None = None,
) -> dict[str, Any]:
    """Queue or reuse the single active Graph execution for one email."""
    active_execution, active_job = await _find_active_email_graph_dispatch(
        session,
        email_id=email_id,
    )
    if active_execution is not None:
        return {
            "workflow": {
                "engine": "langgraph",
                "execution_id": active_execution.execution_id,
                "job_id": active_execution.trigger_job_id,
                "status": active_execution.status,
            },
            "status": "workflow_active",
            "email_id": email_id,
        }
    if active_job is not None:
        active_metadata = active_job.metadata_json or {}
        active_execution_id = str(active_metadata.get("execution_id") or "")
        if not active_execution_id:
            raise RuntimeError("ACTIVE_GRAPH_START_IDENTITY_MISSING")
        return {
            "workflow": {
                "engine": "langgraph",
                "execution_id": active_execution_id,
                "job_id": active_job.id,
                "status": active_job.status,
            },
            "status": "workflow_active",
            "email_id": email_id,
        }
    execution_scope = f"rule-{rule_parse_result_id}" if rule_parse_result_id else f"reparse-{uuid4().hex[:12]}"
    execution_id = workflow_execution_id or f"email-{email_id}-{execution_scope}"
    job = await enqueue_job(
        session,
        job_type="graph_start",
        resource_type="email",
        resource_id=email_id,
        idempotency_key=f"graph_start:{execution_id}",
        metadata={
            "execution_id": execution_id,
            "user_id": user_id,
            "reason": reason,
            "durable_attachment_stages": durable_attachment_stages,
            "rule_parse_result_id": rule_parse_result_id,
        },
        max_attempts=3,
    )
    return {
        "workflow": {
            "engine": "langgraph",
            "execution_id": execution_id,
            "job_id": job.id,
            "status": job.status,
        },
        "status": "queued",
        "email_id": email_id,
    }


_ACTIVE_EMAIL_EXECUTION_STATUSES = {
    "running",
    "waiting_human",
    "waiting_external",
    "resume_queued",
    "failed",
}
_ACTIVE_GRAPH_START_JOB_STATUSES = {
    "queued",
    "retry_wait",
    "running",
    "needs_manual_review",
    "failed",
}


async def _find_active_email_graph_dispatch(
    session: AsyncSession,
    *,
    email_id: int,
) -> tuple[WorkflowExecution | None, JobRunLog | None]:
    """Serialize dispatches and return the current owner of an email workflow."""
    email = await session.get(
        Email,
        email_id,
        with_for_update=True,
        populate_existing=True,
    )
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")
    execution = await session.scalar(
        select(WorkflowExecution)
        .where(
            WorkflowExecution.email_id == email_id,
            WorkflowExecution.execution_mode == "langgraph",
            WorkflowExecution.status.in_(_ACTIVE_EMAIL_EXECUTION_STATUSES),
        )
        .order_by(WorkflowExecution.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if execution is not None:
        return execution, None
    job = await session.scalar(
        select(JobRunLog)
        .where(
            JobRunLog.job_type == "graph_start",
            JobRunLog.resource_type == "email",
            JobRunLog.resource_id == email_id,
            JobRunLog.status.in_(_ACTIVE_GRAPH_START_JOB_STATUSES),
        )
        .order_by(JobRunLog.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    return None, job


def _use_langgraph_for_email(email_id: int) -> bool:
    if settings.WORKFLOW_ENGINE != "langgraph":
        return False
    if email_id in settings.LANGGRAPH_EMAIL_ALLOWLIST:
        return True
    # Hash the immutable ID for a stable but well-distributed rollout bucket;
    # retries and manual reparse cannot cross orchestration engines.
    bucket = int(sha256_text(f"email:{email_id}")[:8], 16) % 100
    return bucket < settings.LANGGRAPH_ROLLOUT_PERCENT


async def merge_threads(
    session: AsyncSession,
    *,
    source_thread_id: int,
    target_thread_id: int,
    user_id: int,
    reason: str | None = None,
) -> dict[str, Any]:
    del session, source_thread_id, target_thread_id, user_id, reason
    raise HTTPException(status_code=status.HTTP_410_GONE, detail="EMAIL_THREAD_MERGE_FORBIDDEN")


async def split_thread(
    session: AsyncSession,
    *,
    source_thread_id: int,
    email_ids: list[int],
    user_id: int,
    reason: str | None = None,
) -> dict[str, Any]:
    source = await session.get(EmailThread, source_thread_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_THREAD_NOT_FOUND")
    emails = (
        await session.execute(select(Email).where(Email.thread_id == source.id, Email.id.in_(email_ids)).order_by(Email.received_at.asc(), Email.id.asc()))
    ).scalars().all()
    if not emails:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EMAIL_THREAD_SPLIT_EMPTY")
    first_email = emails[0]
    new_thread = EmailThread(
        thread_key=sha256_text(f"split:{source.id}:{','.join(str(email_id) for email_id in email_ids)}")[:128],
        normalized_subject=first_email.normalized_subject,
        root_message_id=first_email.message_id,
        latest_email_id=emails[-1].id,
        ticket_id=source.ticket_id,
        email_count=len(emails),
        merge_confidence=1.0000,
        merge_reason=reason or "人工拆分线程。",
        manual_locked=True,
    )
    session.add(new_thread)
    await session.flush()
    for email in emails:
        email.thread_id = new_thread.id
    remaining_count = int(await session.scalar(select(func.count()).select_from(Email).where(Email.thread_id == source.id)) or 0)
    source.email_count = remaining_count
    await log_operation(
        session,
        user_id=user_id,
        operation_type="email_thread_split",
        target_type="email_thread",
        target_id=source.id,
        description=reason,
        after_data={"new_thread_id": new_thread.id, "moved_email_ids": email_ids},
    )
    return {
        "source_thread": model_to_dict(source, ("id", "thread_key", "email_count")),
        "new_thread": model_to_dict(new_thread, ("id", "thread_key", "email_count", "latest_email_id", "merge_reason")),
    }
