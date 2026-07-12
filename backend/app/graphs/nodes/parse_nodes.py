from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.parser import classify_email, extract_fields
from app.services.emails import get_email_detail

logger = logging.getLogger(__name__)


async def load_email_node(session: AsyncSession, state: dict[str, Any]) -> dict[str, Any]:
    email_detail = await get_email_detail(session, state["email_id"])
    email_data = email_detail["email"]
    body = email_data.get("clean_body") or email_data.get("text_body") or ""
    return {
        "email_subject": email_data.get("subject"),
        "email_body": body,
        "email_from": email_data.get("from_address"),
        "email_to": email_data.get("to_addresses"),
        "email_message_id": email_data.get("message_id"),
        "email_in_reply_to": email_data.get("in_reply_to"),
        "email_thread_id": email_data.get("thread_id"),
    }


async def rule_extract_node(session: AsyncSession, state: dict[str, Any]) -> dict[str, Any]:
    body = state.get("email_body", "")
    subject = state.get("email_subject", "")
    from_addr = state.get("email_from", "")
    in_reply_to = state.get("email_in_reply_to")

    class _MockEmail:
        pass

    mock_email = _MockEmail()
    mock_email.subject = subject
    mock_email.clean_body = body
    mock_email.from_address = from_addr
    mock_email.in_reply_to = in_reply_to

    intent_type, confidence, reason = classify_email(mock_email, body)
    extracted = extract_fields(mock_email)

    fields = extracted.get("fields", {})
    items = extracted.get("items", [])
    missing = extracted.get("missing_fields", {})
    conflict = extracted.get("conflict_fields", {})
    evidence = extracted.get("evidence", {})
    rule_confidence = float(extracted.get("confidence_score", 0))

    return {
        "intent_type": intent_type,
        "classification_confidence": confidence,
        "classification_reason": reason,
        "rule_parse_result": {
            "intent_type": intent_type,
            "fields": fields,
            "items": items,
            "missing_fields": missing,
            "conflict_fields": conflict,
            "evidence": evidence,
            "confidence_score": rule_confidence,
        },
        "skip_ai": intent_type in {"irrelevant", "unknown"},
    }
