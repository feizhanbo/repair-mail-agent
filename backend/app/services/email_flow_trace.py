from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    AiCallLog,
    Email,
    EmailAttachment,
    EmailTicketLink,
    FieldAuditLog,
    ManualReviewTask,
    NotificationEvent,
    OperationLog,
    ParseResult,
    RepairTicket,
    RepairTicketItem,
    ReplyRecord,
    SnValidationResult,
    TicketStatusLog,
)
from app.services.common import model_to_dict
from app.services.tickets import EMAIL_FIELDS


def _mask_url_password(url: str) -> str:
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", url)


ATTACHMENT_FIELDS = (
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
)
PARSE_RESULT_FIELDS = (
    "id",
    "email_id",
    "source_attachment_id",
    "ticket_id",
    "parser_type",
    "parser_version",
    "intent_type",
    "extracted_fields",
    "extracted_items",
    "missing_fields",
    "conflict_fields",
    "confidence_score",
    "field_confidences",
    "evidence",
    "apply_status",
    "applied_by_user_id",
    "applied_at",
    "accepted",
    "accepted_by_user_id",
    "accepted_at",
    "error_message",
    "created_at",
)
EMAIL_TICKET_LINK_FIELDS = (
    "id",
    "email_id",
    "ticket_id",
    "link_type",
    "link_reason",
    "linked_by_user_id",
    "created_at",
)
TICKET_FIELDS = (
    "id",
    "ticket_no",
    "current_status_code",
    "source_email_id",
    "thread_id",
    "customer_code",
    "customer_name",
    "contact_person",
    "contact_phone",
    "contact_email",
    "request_date",
    "mailing_address",
    "problem_description",
    "accessories",
    "missing_fields",
    "conflict_fields",
    "confidence_score",
    "followup_count",
    "max_followup_count",
    "assigned_user_id",
    "manual_locked",
    "version",
    "created_at",
    "updated_at",
)
ITEM_FIELDS = (
    "id",
    "ticket_id",
    "line_no",
    "material_code",
    "material_name",
    "sn",
    "sn_asset_id",
    "quantity",
    "failure_description",
    "failure_information",
    "data_info",
    "remarks",
    "accessories",
    "validation_status",
    "validation_message",
    "manual_locked",
    "created_at",
    "updated_at",
)
TASK_FIELDS = (
    "id",
    "ticket_id",
    "email_id",
    "task_type",
    "priority",
    "status",
    "description",
    "trigger_reason",
    "assigned_user_id",
    "claimed_by_user_id",
    "claimed_at",
    "resolved_by_user_id",
    "resolved_at",
    "resolution",
    "created_at",
    "updated_at",
)
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
AI_LOG_FIELDS = (
    "id",
    "trace_id",
    "email_id",
    "ticket_id",
    "call_type",
    "provider_name",
    "model_name",
    "prompt_version",
    "input_summary",
    "output_summary",
    "parsed_key_result",
    "confidence_score",
    "latency_ms",
    "status",
    "error_message",
    "log_file_path",
    "log_line_no",
    "created_at",
)
NOTIFICATION_FIELDS = (
    "id",
    "event_type",
    "target_type",
    "target_id",
    "title",
    "content",
    "priority",
    "recipient_user_id",
    "recipient_role_code",
    "delivery_channel",
    "delivery_status",
    "read_at",
    "metadata_json",
    "delivered_at",
    "created_at",
)
STATUS_LOG_FIELDS = (
    "id",
    "ticket_id",
    "from_status_code",
    "to_status_code",
    "trigger_event",
    "reason",
    "operator_type",
    "operator_user_id",
    "metadata_json",
    "created_at",
)
FIELD_AUDIT_FIELDS = (
    "id",
    "ticket_id",
    "ticket_item_id",
    "field_name",
    "old_value",
    "new_value",
    "source_type",
    "reason",
    "operator_user_id",
    "parse_result_id",
    "created_at",
)
SN_VALIDATION_FIELDS = (
    "id",
    "ticket_id",
    "ticket_item_id",
    "sn",
    "matched_sn_asset_id",
    "check_exists",
    "check_valid",
    "check_customer_match",
    "check_material_match",
    "need_ship_to_beijing",
    "result_status",
    "result_message",
    "checked_by",
    "checked_at",
)
OPERATION_LOG_FIELDS = (
    "id",
    "user_id",
    "operation_type",
    "target_type",
    "target_id",
    "description",
    "before_data",
    "after_data",
    "created_at",
)


async def build_email_flow_trace(
    session: AsyncSession,
    *,
    email_id: int,
    ingest_result: dict[str, Any] | None = None,
    include_database_url: bool = False,
) -> dict[str, Any]:
    email = await session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")

    attachments = (
        await session.execute(select(EmailAttachment).where(EmailAttachment.email_id == email.id).order_by(EmailAttachment.created_at.asc(), EmailAttachment.id.asc()))
    ).scalars().all()
    links = (
        await session.execute(select(EmailTicketLink).where(EmailTicketLink.email_id == email.id).order_by(EmailTicketLink.created_at.asc(), EmailTicketLink.id.asc()))
    ).scalars().all()
    ticket_ids = sorted({link.ticket_id for link in links})
    parse_results = (
        await session.execute(select(ParseResult).where(ParseResult.email_id == email.id).order_by(ParseResult.created_at.asc(), ParseResult.id.asc()))
    ).scalars().all()

    tickets = []
    items = []
    tasks = []
    replies = []
    ai_logs = []
    notifications = []
    status_logs = []
    field_audits = []
    sn_validation_results = []
    operation_logs = []
    if ticket_ids:
        tickets = (await session.execute(select(RepairTicket).where(RepairTicket.id.in_(ticket_ids)).order_by(RepairTicket.id.asc()))).scalars().all()
        items = (
            await session.execute(
                select(RepairTicketItem).where(RepairTicketItem.ticket_id.in_(ticket_ids)).order_by(RepairTicketItem.ticket_id.asc(), RepairTicketItem.line_no.asc())
            )
        ).scalars().all()
        tasks = (
            await session.execute(select(ManualReviewTask).where(ManualReviewTask.ticket_id.in_(ticket_ids)).order_by(ManualReviewTask.created_at.asc(), ManualReviewTask.id.asc()))
        ).scalars().all()
        replies = (
            await session.execute(select(ReplyRecord).where(ReplyRecord.ticket_id.in_(ticket_ids)).order_by(ReplyRecord.created_at.asc(), ReplyRecord.id.asc()))
        ).scalars().all()
        ai_logs = (
            await session.execute(
                select(AiCallLog)
                .where((AiCallLog.email_id == email.id) | (AiCallLog.ticket_id.in_(ticket_ids)))
                .order_by(AiCallLog.created_at.asc(), AiCallLog.id.asc())
            )
        ).scalars().all()
        status_logs = (
            await session.execute(select(TicketStatusLog).where(TicketStatusLog.ticket_id.in_(ticket_ids)).order_by(TicketStatusLog.created_at.asc(), TicketStatusLog.id.asc()))
        ).scalars().all()
        field_audits = (
            await session.execute(select(FieldAuditLog).where(FieldAuditLog.ticket_id.in_(ticket_ids)).order_by(FieldAuditLog.created_at.asc(), FieldAuditLog.id.asc()))
        ).scalars().all()
        sn_validation_results = (
            await session.execute(select(SnValidationResult).where(SnValidationResult.ticket_id.in_(ticket_ids)).order_by(SnValidationResult.checked_at.asc(), SnValidationResult.id.asc()))
        ).scalars().all()
        task_ids = [task.id for task in tasks]
        if task_ids:
            notifications = (
                await session.execute(
                    select(NotificationEvent)
                    .where(NotificationEvent.target_type == "manual_review_task", NotificationEvent.target_id.in_(task_ids))
                    .order_by(NotificationEvent.created_at.asc(), NotificationEvent.id.asc())
                )
            ).scalars().all()
    else:
        ai_logs = (
            await session.execute(select(AiCallLog).where(AiCallLog.email_id == email.id).order_by(AiCallLog.created_at.asc(), AiCallLog.id.asc()))
        ).scalars().all()

    parse_result_ids = [row.id for row in parse_results]
    reply_ids = [row.id for row in replies]
    task_ids = [row.id for row in tasks]
    operation_conditions = [
        (OperationLog.target_type == "email") & (OperationLog.target_id == email.id),
    ]
    if ticket_ids:
        operation_conditions.append((OperationLog.target_type == "repair_ticket") & (OperationLog.target_id.in_(ticket_ids)))
    if parse_result_ids:
        operation_conditions.append((OperationLog.target_type == "parse_result") & (OperationLog.target_id.in_(parse_result_ids)))
    if reply_ids:
        operation_conditions.append((OperationLog.target_type == "reply_record") & (OperationLog.target_id.in_(reply_ids)))
    if task_ids:
        operation_conditions.append((OperationLog.target_type == "manual_review_task") & (OperationLog.target_id.in_(task_ids)))
    operation_logs = (
        await session.execute(select(OperationLog).where(or_(*operation_conditions)).order_by(OperationLog.created_at.asc(), OperationLog.id.asc()))
    ).scalars().all()

    report = {
        "runtime_config": {
            "auto_send_enabled": settings.AUTO_SEND_ENABLED,
            "reply_send_mode": settings.REPLY_SEND_MODE,
            "auto_send_min_confidence": settings.AUTO_SEND_MIN_CONFIDENCE,
            "confidence_threshold": settings.CONFIDENCE_THRESHOLD,
            "max_follow_up": settings.MAX_FOLLOW_UP,
        },
        "ingest_result": ingest_result,
        "trace_only": ingest_result is None,
        "email": model_to_dict(email, EMAIL_FIELDS),
        "email_id": email.id,
        "attachments": [model_to_dict(row, ATTACHMENT_FIELDS) for row in attachments],
        "email_ticket_links": [model_to_dict(row, EMAIL_TICKET_LINK_FIELDS) for row in links],
        "parse_results": [model_to_dict(row, PARSE_RESULT_FIELDS) for row in parse_results],
        "tickets": [model_to_dict(row, TICKET_FIELDS) for row in tickets],
        "ticket_items": [model_to_dict(row, ITEM_FIELDS) for row in items],
        "manual_review_tasks": [model_to_dict(row, TASK_FIELDS) for row in tasks],
        "reply_records": [model_to_dict(row, REPLY_FIELDS) for row in replies],
        "ai_call_logs": [model_to_dict(row, AI_LOG_FIELDS) for row in ai_logs],
        "notification_events": [model_to_dict(row, NOTIFICATION_FIELDS) for row in notifications],
        "ticket_status_logs": [model_to_dict(row, STATUS_LOG_FIELDS) for row in status_logs],
        "field_audit_logs": [model_to_dict(row, FIELD_AUDIT_FIELDS) for row in field_audits],
        "sn_validation_results": [model_to_dict(row, SN_VALIDATION_FIELDS) for row in sn_validation_results],
        "operation_logs": [model_to_dict(row, OPERATION_LOG_FIELDS) for row in operation_logs],
    }
    if include_database_url:
        report["database_url"] = _mask_url_password(settings.DATABASE_URL)
    return report
