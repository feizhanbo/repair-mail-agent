from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Email, ParseResult
from app.services.tickets import apply_parse_result, ensure_manual_review_ticket_from_parse_result

logger = logging.getLogger(__name__)


async def apply_ticket_service_node(session: AsyncSession, state: dict[str, Any]) -> dict[str, Any]:
    email_id = state["email_id"]
    email = await session.get(Email, email_id)
    ai_result = state.get("ai_parse_result")
    rule_result = state.get("rule_parse_result")

    parse_result_id = None
    if ai_result and ai_result.get("parse_result_id"):
        parse_result_id = ai_result["parse_result_id"]
    elif rule_result:
        rule_parse = await session.scalar(
            select(ParseResult).where(
                ParseResult.email_id == email_id,
                ParseResult.parser_type == "rule",
            ).order_by(ParseResult.created_at.desc()).limit(1)
        )
        if rule_parse is not None:
            parse_result_id = rule_parse.id

    if parse_result_id is None:
        return {"requires_manual": True, "manual_review_reason": "无有效解析结果"}

    parse_result = await session.get(ParseResult, parse_result_id)
    if parse_result is None:
        return {"requires_manual": True, "manual_review_reason": "解析结果不存在"}

    needs_manual = state.get("requires_manual", False)
    if needs_manual:
        if email is None:
            return {"error_message": "邮件不存在"}
        reason = state.get("manual_review_reason") or "需要人工复核"
        ticket = await ensure_manual_review_ticket_from_parse_result(
            session,
            email=email,
            parse_result=parse_result,
            reason=reason,
            task_type="ai_review_required",
        )
        return {
            "ticket_id": ticket.id,
            "ticket_no": ticket.ticket_no,
            "current_status_code": ticket.current_status_code,
            "manual_review_reason": reason,
        }

    applied = await apply_parse_result(
        session,
        parse_result_id=parse_result.id,
        user_id=state.get("user_id"),
        reason=state.get("reason", "Graph 自动应用"),
        apply_status="auto_applied",
    )

    ticket = applied.get("ticket") if isinstance(applied, dict) else None
    if ticket is None and isinstance(applied, dict):
        return {"error_message": "应用解析结果失败，未返回工单信息"}

    if ticket is None:
        return {"error_message": "应用解析结果失败"}

    return {
        "ticket_id": ticket.get("id") if isinstance(ticket, dict) else ticket.id,
        "ticket_no": ticket.get("ticket_no") if isinstance(ticket, dict) else ticket.ticket_no,
        "current_status_code": ticket.get("current_status_code") if isinstance(ticket, dict) else ticket.current_status_code,
    }


async def finalize_node(session: AsyncSession, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "graph_completed": True,
        "summary": {
            "ticket_id": state.get("ticket_id"),
            "ticket_no": state.get("ticket_no"),
            "status": state.get("current_status_code"),
            "intent": state.get("intent_type"),
            "confidence": state.get("confidence_score"),
        },
    }
