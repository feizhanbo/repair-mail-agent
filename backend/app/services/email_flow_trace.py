from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiCallLog,
    Email,
    EmailAttachment,
    EmailTicketLink,
    FieldAuditLog,
    JobRunLog,
    MailFetchRecord,
    ManualReviewTask,
    OperationLog,
    OssObject,
    ParseResult,
    RepairTicket,
    ReplyRecord,
    SnValidationResult,
    SystemEventLog,
    TicketStatusLog,
)
from app.services.common import model_to_dict
from app.services.emails import get_email_detail


AI_LOG_FIELDS = (
    "id",
    "trace_id",
    "correlation_id",
    "email_id",
    "ticket_id",
    "attachment_id",
    "job_run_id",
    "call_type",
    "provider_name",
    "model_name",
    "prompt_version",
    "input_summary",
    "output_summary",
    "parsed_key_result",
    "confidence_score",
    "latency_ms",
    "attempt_count",
    "error_code",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "status",
    "created_at",
)


def _event(
    *,
    created_at,
    stage: str,
    event: str,
    event_status: str | None,
    source_type: str,
    source_id: int | None,
    correlation_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "created_at": created_at,
        "stage": stage,
        "event": event,
        "event_type": event,
        "status": event_status,
        "source_type": source_type,
        "source_id": source_id,
        "correlation_id": correlation_id,
        "details": details or {},
        "summary": f"{event}:{event_status}" if event_status else event,
    }


def _sort_timeline(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(timeline, key=lambda item: (item.get("created_at") or "", item.get("source_id") or 0))


async def build_email_flow_trace(session: AsyncSession, email_id: int) -> dict[str, Any]:
    email = await session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")

    detail = await get_email_detail(session, email_id)
    links = (
        await session.execute(select(EmailTicketLink).where(EmailTicketLink.email_id == email_id).order_by(EmailTicketLink.created_at.asc()))
    ).scalars().all()
    ticket_ids = sorted({link.ticket_id for link in links})
    attachments = (
        await session.execute(select(EmailAttachment).where(EmailAttachment.email_id == email_id).order_by(EmailAttachment.created_at.asc()))
    ).scalars().all()
    attachment_ids = [item.id for item in attachments]
    object_ids = [item.oss_object_id for item in attachments if item.oss_object_id]
    if email.raw_eml_oss_object_id:
        object_ids.append(email.raw_eml_oss_object_id)

    fetch_records = (
        await session.execute(select(MailFetchRecord).where(MailFetchRecord.email_id == email_id).order_by(MailFetchRecord.created_at.asc()))
    ).scalars().all()
    parse_results = (
        await session.execute(select(ParseResult).where(ParseResult.email_id == email_id).order_by(ParseResult.created_at.asc()))
    ).scalars().all()
    ai_logs = (
        await session.execute(
            select(AiCallLog)
            .where(or_(AiCallLog.email_id == email_id, AiCallLog.attachment_id.in_(attachment_ids or [-1])))
            .order_by(AiCallLog.created_at.asc(), AiCallLog.id.asc())
        )
    ).scalars().all()
    system_events = (
        await session.execute(
            select(SystemEventLog)
            .where(or_(SystemEventLog.email_id == email_id, SystemEventLog.correlation_id == email.processing_trace_id))
            .order_by(SystemEventLog.created_at.asc())
        )
    ).scalars().all()
    operations = (
        await session.execute(
            select(OperationLog)
            .where(or_(
                OperationLog.email_id == email_id,
                OperationLog.correlation_id == email.processing_trace_id,
                (OperationLog.target_type == "email") & (OperationLog.target_id == email_id),
            ))
            .order_by(OperationLog.created_at.asc())
        )
    ).scalars().all()
    oss_objects = (
        await session.execute(select(OssObject).where(OssObject.id.in_(object_ids or [-1])).order_by(OssObject.created_at.asc()))
    ).scalars().all()
    manual_tasks = (
        await session.execute(
            select(ManualReviewTask)
            .where(or_(ManualReviewTask.email_id == email_id, ManualReviewTask.ticket_id.in_(ticket_ids or [-1])))
            .order_by(ManualReviewTask.created_at.asc())
        )
    ).scalars().all()
    replies = (
        await session.execute(
            select(ReplyRecord)
            .where(or_(ReplyRecord.related_email_id == email_id, ReplyRecord.ticket_id.in_(ticket_ids or [-1])))
            .order_by(ReplyRecord.created_at.asc())
        )
    ).scalars().all()
    jobs = (
        await session.execute(
            select(JobRunLog)
            .where(or_(JobRunLog.correlation_id == email.processing_trace_id, JobRunLog.id == email.fetch_job_run_id))
            .order_by(JobRunLog.created_at.asc())
        )
    ).scalars().all()

    timeline = [
        _event(
            created_at=email.received_at or email.created_at,
            stage="ingest",
            event="email_received",
            event_status=email.parse_status,
            source_type="emails",
            source_id=email.id,
            correlation_id=email.processing_trace_id,
            details={"message_id": email.message_id, "intent_type": email.intent_type},
        )
    ]
    timeline.extend(
        _event(
            created_at=row.created_at,
            stage="imap_fetch",
            event="imap_uid_processed",
            event_status=row.fetch_status,
            source_type="mail_fetch_records",
            source_id=row.id,
            correlation_id=email.processing_trace_id,
            details={"attempt_count": row.attempt_count, "duplicate": row.duplicate},
        )
        for row in fetch_records
    )
    timeline.extend(
        _event(
            created_at=row.created_at,
            stage="oss_archive",
            event="oss_object",
            event_status=row.upload_status,
            source_type="oss_objects",
            source_id=row.id,
            correlation_id=email.processing_trace_id,
            details={"source_type": row.source_type, "file_size": row.file_size},
        )
        for row in oss_objects
    )
    timeline.extend(
        _event(
            created_at=row.created_at,
            stage="attachment_parse",
            event="attachment_processed",
            event_status=row.parse_status,
            source_type="email_attachments",
            source_id=row.id,
            correlation_id=email.processing_trace_id,
            details={"file_name": row.file_name, "content_type": row.content_type, "error_code": row.parse_error},
        )
        for row in attachments
    )
    timeline.extend(
        _event(
            created_at=row.created_at,
            stage="parse",
            event=f"parse_{row.parser_type}",
            event_status=row.apply_status,
            source_type="parse_results",
            source_id=row.id,
            correlation_id=email.processing_trace_id,
            details={"intent_type": row.intent_type, "confidence_score": row.confidence_score},
        )
        for row in parse_results
    )
    timeline.extend(
        _event(
            created_at=row.created_at,
            stage="ai",
            event=row.call_type,
            event_status=row.status,
            source_type="ai_call_logs",
            source_id=row.id,
            correlation_id=row.correlation_id or email.processing_trace_id,
            details={"provider": row.provider_name, "model": row.model_name, "error_code": row.error_code, "attempt": row.attempt_count},
        )
        for row in ai_logs
    )
    timeline.extend(
        _event(
            created_at=row.created_at,
            stage=row.event_stage or "system",
            event=row.event_type,
            event_status=row.event_status,
            source_type="system_event_logs",
            source_id=row.id,
            correlation_id=row.correlation_id,
            details={"severity": row.severity, "error_code": row.error_code, "duration_ms": row.duration_ms},
        )
        for row in system_events
    )
    timeline.extend(
        _event(
            created_at=row.created_at,
            stage="operation",
            event=row.operation_type,
            event_status="recorded",
            source_type="operation_logs",
            source_id=row.id,
            correlation_id=row.correlation_id,
            details={"user_id": row.user_id, "target_type": row.target_type, "target_id": row.target_id},
        )
        for row in operations
    )
    timeline.extend(
        _event(
            created_at=row.created_at,
            stage="manual_review",
            event=row.task_type,
            event_status=row.status,
            source_type="manual_review_tasks",
            source_id=row.id,
            correlation_id=email.processing_trace_id,
            details={"ticket_id": row.ticket_id, "priority": row.priority},
        )
        for row in manual_tasks
    )
    timeline.extend(
        _event(
            created_at=row.created_at,
            stage="reply",
            event=row.reply_type,
            event_status=row.send_status,
            source_type="reply_records",
            source_id=row.id,
            correlation_id=email.processing_trace_id,
            details={"ticket_id": row.ticket_id, "review_status": row.review_status},
        )
        for row in replies
    )
    timeline.extend(
        _event(
            created_at=row.created_at,
            stage="job",
            event=row.job_type,
            event_status=row.status,
            source_type="job_run_logs",
            source_id=row.id,
            correlation_id=row.correlation_id,
            details={"attempt_count": row.attempt_count, "error_code": row.error_code},
        )
        for row in jobs
    )

    return {
        **detail,
        "processing_trace_id": email.processing_trace_id,
        "ticket_links": [
            model_to_dict(link, ("id", "email_id", "ticket_id", "link_type", "link_reason", "linked_by_user_id", "created_at"))
            for link in links
        ],
        "ai_logs": [model_to_dict(row, AI_LOG_FIELDS) for row in ai_logs],
        "timeline": _sort_timeline(timeline),
    }


async def build_ticket_timeline(session: AsyncSession, ticket_id: int) -> dict[str, Any]:
    ticket = await session.get(RepairTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")

    links = (await session.execute(select(EmailTicketLink).where(EmailTicketLink.ticket_id == ticket_id))).scalars().all()
    email_ids = [row.email_id for row in links]
    status_logs = (await session.execute(select(TicketStatusLog).where(TicketStatusLog.ticket_id == ticket_id))).scalars().all()
    field_logs = (await session.execute(select(FieldAuditLog).where(FieldAuditLog.ticket_id == ticket_id))).scalars().all()
    validations = (await session.execute(select(SnValidationResult).where(SnValidationResult.ticket_id == ticket_id))).scalars().all()
    tasks = (await session.execute(select(ManualReviewTask).where(ManualReviewTask.ticket_id == ticket_id))).scalars().all()
    replies = (await session.execute(select(ReplyRecord).where(ReplyRecord.ticket_id == ticket_id))).scalars().all()
    ai_logs = (await session.execute(select(AiCallLog).where(AiCallLog.ticket_id == ticket_id))).scalars().all()
    operations = (
        await session.execute(
            select(OperationLog).where(or_(
                OperationLog.ticket_id == ticket_id,
                OperationLog.email_id.in_(email_ids or [-1]),
                (OperationLog.target_type.in_(["ticket", "repair_ticket"])) & (OperationLog.target_id == ticket_id),
            ))
        )
    ).scalars().all()
    events = (await session.execute(select(SystemEventLog).where(SystemEventLog.ticket_id == ticket_id))).scalars().all()

    timeline: list[dict[str, Any]] = []
    for row in links:
        timeline.append(_event(created_at=row.created_at, stage="thread", event="email_linked", event_status=row.link_type, source_type="email_ticket_links", source_id=row.id, details={"email_id": row.email_id}))
    for row in status_logs:
        timeline.append(_event(created_at=row.created_at, stage="ticket_status", event=row.trigger_event, event_status=row.to_status_code, source_type="ticket_status_logs", source_id=row.id, details={"from_status": row.from_status_code, "operator_type": row.operator_type}))
    for row in field_logs:
        timeline.append(_event(created_at=row.created_at, stage="field_change", event="field_updated", event_status="recorded", source_type="field_audit_logs", source_id=row.id, details={"field_name": row.field_name, "source_type": row.source_type}))
    for row in validations:
        timeline.append(_event(created_at=row.checked_at, stage="sn_validation", event="sn_validated", event_status=row.result_status, source_type="sn_validation_results", source_id=row.id, details={"checked_by": row.checked_by}))
    for row in tasks:
        timeline.append(_event(created_at=row.created_at, stage="manual_review", event=row.task_type, event_status=row.status, source_type="manual_review_tasks", source_id=row.id, details={"priority": row.priority, "email_id": row.email_id}))
    for row in replies:
        timeline.append(_event(created_at=row.created_at, stage="reply", event=row.reply_type, event_status=row.send_status, source_type="reply_records", source_id=row.id, details={"review_status": row.review_status, "related_email_id": row.related_email_id}))
    for row in ai_logs:
        timeline.append(_event(created_at=row.created_at, stage="ai", event=row.call_type, event_status=row.status, source_type="ai_call_logs", source_id=row.id, correlation_id=row.correlation_id, details={"provider": row.provider_name, "model": row.model_name, "error_code": row.error_code}))
    for row in operations:
        timeline.append(_event(created_at=row.created_at, stage="operation", event=row.operation_type, event_status="recorded", source_type="operation_logs", source_id=row.id, correlation_id=row.correlation_id, details={"user_id": row.user_id, "target_type": row.target_type}))
    for row in events:
        timeline.append(_event(created_at=row.created_at, stage=row.event_stage or "system", event=row.event_type, event_status=row.event_status, source_type="system_event_logs", source_id=row.id, correlation_id=row.correlation_id, details={"severity": row.severity, "error_code": row.error_code}))

    return {
        "ticket_id": ticket.id,
        "ticket_no": ticket.ticket_no,
        "current_status_code": ticket.current_status_code,
        "timeline": _sort_timeline(timeline),
    }
