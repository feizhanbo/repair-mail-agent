from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Email, EmailAttachment
from app.services.ai import create_ai_parse_candidate

logger = logging.getLogger(__name__)


async def ai_full_parse_node(session: AsyncSession, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("skip_ai", False):
        return {"ai_parse_result": None}

    email = await session.get(Email, state["email_id"])
    if email is None:
        return {"ai_parse_result": None, "requires_manual": True, "error_message": "Email not found"}

    attachments = (
        await session.execute(
            select(EmailAttachment).where(EmailAttachment.email_id == email.id).order_by(EmailAttachment.created_at.desc())
        )
    ).scalars().all()

    result = await create_ai_parse_candidate(
        session,
        email=email,
        attachments=list(attachments),
        mode="classification_and_extract",
        rule_context=state.get("rule_parse_result"),
    )

    if result is None:
        return {"ai_parse_result": None, "requires_manual": True}

    parse_result = result.get("parse_result")
    if parse_result is None:
        return {"ai_parse_result": None, "requires_manual": True}

    return {
        "ai_parse_result": {
            "parse_result_id": parse_result.id,
            "intent_type": parse_result.intent_type,
            "fields": parse_result.extracted_fields,
            "items": parse_result.extracted_items,
            "missing_fields": parse_result.missing_fields,
            "conflict_fields": parse_result.conflict_fields,
            "confidence_score": float(parse_result.confidence_score or 0),
        },
        "confidence_score": float(parse_result.confidence_score or 0),
        "missing_fields": parse_result.missing_fields,
        "conflict_fields": parse_result.conflict_fields,
    }
