from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok
from app.models import AiCallLog, Email, ManualReviewTask, OperationLog, RepairTicket, ReplyRecord, User

router = APIRouter()


def _default_range(period: str) -> tuple[date, date]:
    today = datetime.utcnow().date()
    if period == "year":
        return today - timedelta(days=365), today
    if period == "month":
        return today - timedelta(days=30), today
    return today - timedelta(days=7), today


def _in_range(column, start_date: date, end_date: date):
    return column >= start_date, column < end_date + timedelta(days=1)


async def _count(session: AsyncSession, statement) -> int:
    return int(await session.scalar(statement) or 0)


@router.get("/summary")
async def statistics_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
    period: Annotated[str, Query(pattern="^(week|month|year)$")] = "week",
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    del current_user
    range_start, range_end = (start_date, end_date) if start_date and end_date else _default_range(period)

    email_count = await _count(session, select(func.count()).select_from(Email).where(*_in_range(Email.created_at, range_start, range_end)))
    ticket_count = await _count(session, select(func.count()).select_from(RepairTicket).where(*_in_range(RepairTicket.created_at, range_start, range_end)))
    completed_count = await _count(
        session,
        select(func.count())
        .select_from(RepairTicket)
        .where(*_in_range(RepairTicket.updated_at, range_start, range_end), RepairTicket.current_status_code.in_(("ready_for_export", "closed"))),
    )
    reparse_count = await _count(
        session,
        select(func.count())
        .select_from(OperationLog)
        .where(*_in_range(OperationLog.created_at, range_start, range_end), OperationLog.operation_type == "email_reparsed"),
    )

    ai_total = await _count(session, select(func.count()).select_from(AiCallLog).where(*_in_range(AiCallLog.created_at, range_start, range_end)))
    ai_success = await _count(
        session,
        select(func.count())
        .select_from(AiCallLog)
        .where(*_in_range(AiCallLog.created_at, range_start, range_end), AiCallLog.status.in_(("success", "low_confidence"))),
    )
    reply_total = await _count(session, select(func.count()).select_from(ReplyRecord).where(*_in_range(ReplyRecord.created_at, range_start, range_end)))
    auto_reply_total = await _count(
        session,
        select(func.count())
        .select_from(ReplyRecord)
        .where(*_in_range(ReplyRecord.created_at, range_start, range_end), ReplyRecord.generate_source.in_(("ai", "template"))),
    )
    manual_task_total = await _count(
        session,
        select(func.count()).select_from(ManualReviewTask).where(*_in_range(ManualReviewTask.created_at, range_start, range_end)),
    )
    task_pool_total = await _count(
        session,
        select(func.count()).select_from(ManualReviewTask).where(ManualReviewTask.status.in_(("pending", "assigned", "claimed"))),
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
            .where(*_in_range(ManualReviewTask.resolved_at, range_start, range_end))
            .group_by(User.id, User.real_name, User.username)
            .order_by(func.count(ManualReviewTask.id).desc())
        )
    ).all()

    trend: dict[str, dict[str, int]] = defaultdict(lambda: {"emails": 0, "tickets": 0, "completed": 0})
    day = range_start
    while day <= range_end:
        trend[day.isoformat()]
        day += timedelta(days=1)
    email_dates = (await session.execute(select(Email.created_at).where(*_in_range(Email.created_at, range_start, range_end)))).scalars().all()
    for value in email_dates:
        trend[value.date().isoformat()]["emails"] += 1
    ticket_dates = (await session.execute(select(RepairTicket.created_at).where(*_in_range(RepairTicket.created_at, range_start, range_end)))).scalars().all()
    for value in ticket_dates:
        trend[value.date().isoformat()]["tickets"] += 1
    completed_dates = (
        await session.execute(
            select(RepairTicket.updated_at).where(
                *_in_range(RepairTicket.updated_at, range_start, range_end),
                RepairTicket.current_status_code.in_(("ready_for_export", "closed")),
            )
        )
    ).scalars().all()
    for value in completed_dates:
        trend[value.date().isoformat()]["completed"] += 1

    return ok(
        {
            "period": period,
            "start_date": range_start.isoformat(),
            "end_date": range_end.isoformat(),
            "email_count": email_count,
            "ticket_count": ticket_count,
            "completed_count": completed_count,
            "reparse_count": reparse_count,
            "ai_success_rate": round((ai_success / ai_total) * 100, 2) if ai_total else 0,
            "auto_reply_rate": round((auto_reply_total / reply_total) * 100, 2) if reply_total else 0,
            "manual_intervention_rate": round((manual_task_total / ticket_count) * 100, 2) if ticket_count else 0,
            "task_pool_total": task_pool_total,
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
            "trend": [{"date": key, **value} for key, value in sorted(trend.items())],
        }
    )
