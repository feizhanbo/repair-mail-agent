from __future__ import annotations

import re
from datetime import date
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.request_context import get_correlation_id
from app.models import Email, EmailAttachment, EmailThread, ParseResult, RepairTicket
from app.schemas.business import EmailIngestRequest
from app.services.ai import create_ai_parse_candidate
from app.services.attachment_parser import parse_attachment
from app.services.audit import log_operation, log_system_event
from app.services.common import address_domain, model_to_dict, normalize_message_id, normalize_subject, paginate_scalars, sha256_text, to_plain, utcnow
from app.services.parser import (
    RuleAnalysisResult,
    analyze_email_rules,
    extract_latest_reply_segment,
    html_to_text,
    normalize_email_body,
)
from app.services.replies import create_reply_draft
from app.services.tickets import EMAIL_FIELDS, apply_parse_result, ensure_manual_review_ticket_from_parse_result, serialize_email, serialize_parse_result, validate_ticket_sn


async def list_emails(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    parse_status: str | None = None,
    intent_type: str | None = None,
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
        "attachments": [
            model_to_dict(
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
            for attachment in attachments
        ],
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
    if references_header:
        for reference_id in reversed(re.findall(r"<[^<>]+>", references_header)):
            parent = await session.scalar(select(Email).where(Email.message_id == reference_id))
            if parent and parent.thread_id:
                thread = await session.get(EmailThread, parent.thread_id)
                if thread:
                    thread.merge_confidence = 0.9500
                    thread.merge_reason = "References matched an existing email."
                    return thread
    if in_reply_to:
        parent = await session.scalar(select(Email).where(Email.message_id == in_reply_to))
        if parent and parent.thread_id:
            thread = await session.get(EmailThread, parent.thread_id)
            if thread:
                thread.merge_confidence = 0.9500
                thread.merge_reason = "In-Reply-To 精确命中已入库邮件。"
                return thread
    if normalized_subject and from_domain:
        thread = await session.scalar(
            select(EmailThread)
            .join(Email, Email.id == EmailThread.latest_email_id)
            .where(EmailThread.normalized_subject == normalized_subject)
            .where(Email.from_domain == from_domain)
            .order_by(EmailThread.updated_at.desc(), EmailThread.id.desc())
        )
        if thread:
            thread.merge_confidence = 0.6500
            thread.merge_reason = "归一化主题命中，等待后续规则或人工确认。"
            return thread
    thread = EmailThread(
        thread_key=sha256_text(f"{message_id}:{normalized_subject or ''}")[:128],
        normalized_subject=normalized_subject,
        root_message_id=message_id,
        email_count=0,
        merge_confidence=1.0000,
        merge_reason="新建邮件线程。",
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
        raw_headers={"raw_eml_sha256": payload.raw_eml_sha256} if payload.raw_eml_sha256 else None,
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
        created_at=created_at,
        updated_at=created_at,
    )
    analysis = rule_analysis or analyze_email_rules(email)
    email.intent_type = analysis.intent_type
    if analysis.intent_type == "irrelevant":
        email.parse_status = "skipped"
    session.add(email)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="EMAIL_DUPLICATE") from exc
    thread.latest_email_id = email.id
    thread.email_count = (thread.email_count or 0) + 1

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
        result["parse"] = await reparse_email(
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
    return (
        parse_result.intent_type in {None, "", "unknown"}
        or confidence < settings.AUTO_APPLY_MIN_CONFIDENCE
        or bool(parse_result.missing_fields)
        or bool(parse_result.conflict_fields)
        or any(
            attachment.parse_status in {"needs_manual_review", "unsupported", "failed"}
            for attachment in (attachments or [])
        )
    )


def _manual_reason(parse_result: ParseResult | None) -> str:
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
    return "；".join(reasons) or "需要人工复核 AI 解析结果。"


def _reply_type_for_parse(parse_result: ParseResult | None) -> str:
    if parse_result and parse_result.conflict_fields and "sn" in parse_result.conflict_fields:
        return "sn_invalid"
    if parse_result and parse_result.missing_fields:
        return "missing_fields"
    return "receipt"


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
    try:
        return await create_reply_draft(
            session,
            ticket_id=ticket_id,
            user_id=user_id,
            reply_type=_reply_type_for_parse(parse_result),
            related_email_id=email_id,
            missing_fields=parse_result.missing_fields if parse_result else None,
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
        parse_payload = analysis.to_parse_payload()
        rule_parse = ParseResult(
            email_id=email.id,
            ticket_id=None,
            parser_type="rule",
            parser_version="pre-archive-v2",
            intent_type=parse_payload["intent_type"],
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
    email.parse_status = "parsing"
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
        if attachment.parse_status in {"parsed", "skipped_decorative", "unsupported"}:
            attachment_result = attachment.extracted_json
        else:
            attachment_result = await parse_attachment(session, attachment)
        if attachment_result:
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
    closed_thread = bool(thread_ticket and thread_ticket.current_status_code == "closed")
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
        },
        multimodal_results=multimodal_results or None,
    )

    ai_parse = ai_result["parse_result"] if ai_result else None
    ai_applied: dict[str, Any] | None = None
    manual_ticket: dict[str, Any] | None = None
    draft_result: dict[str, Any] | None = None

    if isinstance(ai_parse, ParseResult):
        email.intent_type = ai_parse.intent_type or email.intent_type
        if closed_thread:
            closed_reason = "已关闭工单收到新邮件，需要人工判断是否新建工单或重新开启处理。"
            ticket = await ensure_manual_review_ticket_from_parse_result(
                session,
                email=email,
                parse_result=ai_parse,
                reason=closed_reason,
                task_type="closed_thread_new_email",
            )
            email.parse_status = "needs_manual"
            manual_ticket = {"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "reason": closed_reason}
        elif ai_parse.intent_type == "irrelevant" and not _parse_requires_manual(ai_parse, list(attachments)):
            ai_parse.apply_status = "auto_skipped"
            email.parse_status = "skipped"
        elif _parse_requires_manual(ai_parse, list(attachments)):
            ticket = await ensure_manual_review_ticket_from_parse_result(
                session,
                email=email,
                parse_result=ai_parse,
                reason=_manual_reason(ai_parse),
                task_type="ai_review_required",
            )
            email.parse_status = "needs_manual"
            manual_ticket = {"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "reason": _manual_reason(ai_parse)}
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
                validated = await validate_ticket_sn(session, ticket_id=ticket_id, user_id=None)
                ai_applied = validated
                validated_ticket = validated.get("ticket") if isinstance(validated, dict) else None
                validated_status = validated_ticket.get("current_status_code") if isinstance(validated_ticket, dict) else None
                if validated_status != "ready_for_export":
                    validation_reason = "SN 校验未全部通过，AI 结果不得自动采纳。"
                    ticket = await ensure_manual_review_ticket_from_parse_result(
                        session,
                        email=email,
                        parse_result=ai_parse,
                        reason=validation_reason,
                        task_type="sn_validation_required",
                    )
                    email.parse_status = "needs_manual"
                    manual_ticket = {
                        "ticket_id": ticket.id,
                        "ticket_no": ticket.ticket_no,
                        "reason": validation_reason,
                    }
                    draft_result = await _try_create_reply_draft(
                        session,
                        ticket_id=ticket.id,
                        user_id=user_id,
                        email_id=email.id,
                        parse_result=ai_parse,
                    )
                else:
                    draft_result = await _try_create_reply_draft(
                        session,
                        ticket_id=ticket_id,
                        user_id=user_id,
                        email_id=email.id,
                        parse_result=ai_parse,
                    )
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
    return {
        "parse_result": serialize_parse_result(rule_parse),
        "applied": None,
        "ai_parse_result": serialize_parse_result(ai_parse) if isinstance(ai_parse, ParseResult) else None,
        "ai_applied": ai_applied,
        "manual_ticket": manual_ticket,
        "draft_result": draft_result,
    }


async def merge_threads(
    session: AsyncSession,
    *,
    source_thread_id: int,
    target_thread_id: int,
    user_id: int,
    reason: str | None = None,
) -> dict[str, Any]:
    if source_thread_id == target_thread_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EMAIL_THREAD_SAME_TARGET")
    source = await session.get(EmailThread, source_thread_id)
    target = await session.get(EmailThread, target_thread_id)
    if source is None or target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_THREAD_NOT_FOUND")
    emails = (await session.execute(select(Email).where(Email.thread_id == source.id))).scalars().all()
    for email in emails:
        email.thread_id = target.id
    target.email_count = (target.email_count or 0) + len(emails)
    if emails:
        target.latest_email_id = max(emails, key=lambda email: email.received_at or email.created_at).id
    target.merge_confidence = 1.0000
    target.merge_reason = reason or "人工合并线程。"
    source.email_count = 0
    source.manual_locked = True
    await log_operation(
        session,
        user_id=user_id,
        operation_type="email_thread_merged",
        target_type="email_thread",
        target_id=target.id,
        description=reason,
        after_data={"source_thread_id": source.id, "moved_email_count": len(emails)},
    )
    return {
        "source_thread": model_to_dict(source, ("id", "thread_key", "email_count", "manual_locked")),
        "target_thread": model_to_dict(target, ("id", "thread_key", "email_count", "latest_email_id", "merge_reason")),
    }


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
