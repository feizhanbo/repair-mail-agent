from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiCallLog, Email, EmailAttachment, EmailTicketLink, ParseResult
from app.services.common import model_to_dict
from app.services.emails import get_email_detail
from app.services.tickets import serialize_email, serialize_parse_result

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
    "log_record_hash",
    "created_at",
)


async def build_email_flow_trace(session: AsyncSession, email_id: int) -> dict[str, Any]:
    email = await session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")

    detail = await get_email_detail(session, email_id)
    ticket_links = (
        await session.execute(select(EmailTicketLink).where(EmailTicketLink.email_id == email_id).order_by(EmailTicketLink.created_at.asc()))
    ).scalars().all()
    ai_logs = (
        await session.execute(select(AiCallLog).where(AiCallLog.email_id == email_id).order_by(AiCallLog.created_at.asc(), AiCallLog.id.asc()))
    ).scalars().all()
    parse_results = (
        await session.execute(select(ParseResult).where(ParseResult.email_id == email_id).order_by(ParseResult.created_at.asc(), ParseResult.id.asc()))
    ).scalars().all()
    attachments = (
        await session.execute(select(EmailAttachment).where(EmailAttachment.email_id == email_id).order_by(EmailAttachment.created_at.asc()))
    ).scalars().all()

    timeline: list[dict[str, Any]] = [
        {
            "event_type": "email_received",
            "created_at": email.received_at or email.created_at,
            "summary": email.subject or email.message_id,
            "data": serialize_email(email),
        }
    ]
    for attachment in attachments:
        timeline.append(
            {
                "event_type": "attachment_detected",
                "created_at": attachment.created_at,
                "summary": attachment.file_name,
                "data": model_to_dict(
                    attachment,
                    ("id", "oss_object_id", "file_name", "content_type", "file_size", "file_hash", "parse_status"),
                ),
            }
        )
    for parse_result in parse_results:
        timeline.append(
            {
                "event_type": f"parse_{parse_result.parser_type}",
                "created_at": parse_result.created_at,
                "summary": parse_result.intent_type or parse_result.apply_status,
                "data": serialize_parse_result(parse_result),
            }
        )
    for ai_log in ai_logs:
        timeline.append(
            {
                "event_type": "ai_call",
                "created_at": ai_log.created_at,
                "summary": f"{ai_log.call_type}:{ai_log.status}",
                "data": model_to_dict(ai_log, AI_LOG_FIELDS),
            }
        )
    timeline.sort(key=lambda item: item["created_at"] or "")

    return {
        **detail,
        "ticket_links": [
            model_to_dict(link, ("id", "email_id", "ticket_id", "link_type", "link_reason", "linked_by_user_id", "created_at"))
            for link in ticket_links
        ],
        "ai_logs": [model_to_dict(row, AI_LOG_FIELDS) for row in ai_logs],
        "timeline": timeline,
    }
