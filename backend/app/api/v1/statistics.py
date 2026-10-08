from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok
from app.models import AiCallLog, Email, ManualReviewTask, OperationLog, RepairTicket, ReplyRecord, User

router = APIRouter()
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _default_range(period: str) -> tuple[date, date]:
    today = datetime.now(BUSINESS_TZ).date()
    if period == "year":
        return date(today.year, 1, 1), today
    if period == "month":
        return date(today.year, today.month, 1), today
    return today - timedelta(days=today.weekday()), today


def _range_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Convert Shanghai business dates to the naive UTC timestamps stored in MySQL."""
    start = datetime.combine(start_date, time.min, tzinfo=BUSINESS_TZ)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=BUSINESS_TZ)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _in_range(column, start_at: datetime, end_at: datetime):
    return column >= start_at, column < end_at


async def _count(session: AsyncSession, statement) -> int:
    return int(await session.scalar(statement) or 0)


@router.get("/summary")
async def statistics_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
    period: Annotated[str, Query(pattern="^(week|month|year)$")] = "week",
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    del current_user
    if (start_date is None) != (end_date is None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="STATISTICS_DATE_RANGE_INCOMPLETE")
    is_custom_range = bool(start_date and end_date)
    range_start, range_end = (start_date, end_date) if is_custom_range else _default_range(period)
    if range_start > range_end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="STATISTICS_DATE_RANGE_INVALID")
    start_at, end_at = _range_bounds(range_start, range_end)

    email_count = await _count(session, select(func.count()).select_from(Email).where(*_in_range(Email.created_at, start_at, end_at)))
    ticket_count = await _count(session, select(func.count()).select_from(RepairTicket).where(*_in_range(RepairTicket.created_at, start_at, end_at)))
    completed_count = await _count(
        session,
        select(func.count())
        .select_from(RepairTicket)
        .where(*_in_range(RepairTicket.updated_at, start_at, end_at), RepairTicket.current_status_code.in_(("ready_for_export", "closed"))),
    )
    reparse_count = await _count(
        session,
        select(func.count())
        .select_from(OperationLog)
        .where(*_in_range(OperationLog.created_at, start_at, end_at), OperationLog.operation_type == "email_reparsed"),
    )

    ai_range = _in_range(AiCallLog.created_at, start_at, end_at)
    ai_total = await _count(session, select(func.count()).select_from(AiCallLog).where(*ai_range))
    ai_success = await _count(
        session,
        select(func.count())
        .select_from(AiCallLog)
        .where(*ai_range, AiCallLog.status.in_(("success", "low_confidence"))),
    )
    token_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(AiCallLog.input_tokens), 0),
                func.coalesce(func.sum(AiCallLog.output_tokens), 0),
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            AiCallLog.total_tokens,
                            func.coalesce(AiCallLog.input_tokens, 0) + func.coalesce(AiCallLog.output_tokens, 0),
                        )
                    ),
                    0,
                ),
                func.coalesce(func.sum(case((
                    (AiCallLog.total_tokens.is_not(None))
                    | (AiCallLog.input_tokens.is_not(None))
                    | (AiCallLog.output_tokens.is_not(None)),
                    1,
                ), else_=0)), 0),
            ).where(*ai_range)
        )
    ).one()
    ai_metered_call_count = int(token_row[3] or 0)
    reply_total = await _count(
        session,
        select(func.count())
        .select_from(ReplyRecord)
        .where(*_in_range(ReplyRecord.sent_at, start_at, end_at), ReplyRecord.send_status == "sent"),
    )
    auto_reply_total = await _count(
        session,
        select(func.count())
        .select_from(ReplyRecord)
        .where(*_in_range(ReplyRecord.sent_at, start_at, end_at), ReplyRecord.review_status == "auto_approved", ReplyRecord.send_status == "sent"),
    )
    manual_ticket_total = await _count(
        session,
        select(func.count(func.distinct(RepairTicket.id)))
        .select_from(RepairTicket)
        .join(ManualReviewTask, ManualReviewTask.ticket_id == RepairTicket.id)
        .where(
            *_in_range(RepairTicket.created_at, start_at, end_at),
            *_in_range(ManualReviewTask.created_at, start_at, end_at),
        ),
    )
    open_statuses = ("pending", "assigned", "claimed", "assignment_failed")
    task_pool_total = await _count(
        session,
        select(func.count()).select_from(ManualReviewTask).where(ManualReviewTask.status.in_(open_statuses)),
    )
    task_pool_ticket_total = await _count(
        session,
        select(func.count(func.distinct(ManualReviewTask.ticket_id))).where(
            ManualReviewTask.status.in_(open_statuses), ManualReviewTask.ticket_id.is_not(None)
        ),
    )
    task_pool_email_total = await _count(
        session,
        select(func.count()).select_from(ManualReviewTask).where(
            ManualReviewTask.status.in_(open_statuses), ManualReviewTask.ticket_id.is_(None)
        ),
    )
    need_customer_info = await _count(
        session,
        select(func.count()).select_from(RepairTicket).where(RepairTicket.current_status_code == "need_customer_info"),
    )
    error_ticket_count = await _count(
        session,
        select(func.count()).select_from(RepairTicket).where(RepairTicket.current_status_code == "error"),
    )
    ready_for_export = await _count(
        session,
        select(func.count()).select_from(RepairTicket).where(RepairTicket.current_status_code == "ready_for_export"),
    )
    status_rows = (
        await session.execute(select(RepairTicket.current_status_code, func.count()).group_by(RepairTicket.current_status_code))
    ).all()

    user_rows = (
        await session.execute(
            select(User.id, User.real_name, User.username, func.count(ManualReviewTask.id))
            .join(ManualReviewTask, ManualReviewTask.resolved_by_user_id == User.id)
            .where(*_in_range(ManualReviewTask.resolved_at, start_at, end_at))
            .group_by(User.id, User.real_name, User.username)
            .order_by(func.count(ManualReviewTask.id).desc())
        )
    ).all()

    trend = [
        {
            "label": "自定义区间" if is_custom_range else {"week": "本周", "month": "本月", "year": "本年"}.get(period, "自定义区间"),
            "start_date": range_start.isoformat(),
            "end_date": range_end.isoformat(),
            "email_count": email_count,
            "ticket_count": ticket_count,
            "completed_count": completed_count,
            "reparse_count": reparse_count,
        }
    ]

    return ok(
        {
            "period": period,
            "start_date": range_start.isoformat(),
            "end_date": range_end.isoformat(),
            "email_count": email_count,
            "ticket_count": ticket_count,
            "completed_count": completed_count,
            "reparse_count": reparse_count,
            "ai_input_tokens": int(token_row[0] or 0),
            "ai_output_tokens": int(token_row[1] or 0),
            "ai_total_tokens": int(token_row[2] or 0),
            "ai_metered_call_count": ai_metered_call_count,
            "ai_unmetered_call_count": max(0, ai_total - ai_metered_call_count),
            "ai_success_rate": round((ai_success / ai_total) * 100, 2) if ai_total else 0,
            "auto_reply_rate": round((auto_reply_total / reply_total) * 100, 2) if reply_total else 0,
            "manual_intervention_rate": round((manual_ticket_total / ticket_count) * 100, 2) if ticket_count else 0,
            "manual_intervention_ticket_count": manual_ticket_total,
            "task_pool_total": task_pool_total,
            "task_pool_ticket_total": task_pool_ticket_total,
            "task_pool_email_total": task_pool_email_total,
            "need_customer_info": need_customer_info,
            "error_ticket_count": error_ticket_count,
            "ready_for_export": ready_for_export,
            "status_distribution": [
                {"status_code": row[0] or "unknown", "count": int(row[1] or 0)}
                for row in status_rows
            ],
            "user_processing": [
                {"user_id": row[0], "real_name": row[1], "username": row[2], "resolved_count": int(row[3] or 0)}
                for row in user_rows
            ],
            "trend": trend,
        }
    )
