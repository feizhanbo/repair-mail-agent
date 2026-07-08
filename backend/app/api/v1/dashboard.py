from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok
from app.models import AiCallLog, Email, JobRunLog, ManualReviewTask, RepairTicket
from app.services.common import model_to_dict

router = APIRouter()


@router.get("/summary")
async def summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    async def ticket_count(status_code: str) -> int:
        return int(await session.scalar(select(func.count()).select_from(RepairTicket).where(RepairTicket.current_status_code == status_code)) or 0)

    new_emails = int(await session.scalar(select(func.count()).select_from(Email).where(Email.parse_status == "pending")) or 0)
    pending_tasks = int(
        await session.scalar(select(func.count()).select_from(ManualReviewTask).where(ManualReviewTask.status.in_(("pending", "assigned", "claimed"))))
        or 0
    )
    current_user_tasks = int(
        await session.scalar(
            select(func.count())
            .select_from(ManualReviewTask)
            .where(
                ManualReviewTask.status.in_(("pending", "assigned", "claimed")),
                (ManualReviewTask.assigned_user_id == current_user.id) | (ManualReviewTask.claimed_by_user_id == current_user.id),
            )
        )
        or 0
    )
    in_progress_tasks = int(
        await session.scalar(
            select(func.count())
            .select_from(ManualReviewTask)
            .where(ManualReviewTask.status == "claimed", ManualReviewTask.claimed_by_user_id == current_user.id)
        )
        or 0
    )
    all_in_progress_tasks = int(
        await session.scalar(select(func.count()).select_from(ManualReviewTask).where(ManualReviewTask.status == "claimed")) or 0
    )
    resolved_tasks = int(
        await session.scalar(select(func.count()).select_from(ManualReviewTask).where(ManualReviewTask.status == "resolved")) or 0
    )
    low_confidence = int(await session.scalar(select(func.count()).select_from(AiCallLog).where(AiCallLog.status == "low_confidence")) or 0)
    recent_failed_jobs = (
        await session.execute(select(JobRunLog).where(JobRunLog.status.in_(("failed", "error"))).order_by(JobRunLog.started_at.desc()).limit(10))
    ).scalars().all()
    return ok(
        {
            "new_emails": new_emails,
            "pending_parse": new_emails,
            "manual_review": await ticket_count("manual_review"),
            "manual_review_tasks": pending_tasks,
            "task_pool_total": pending_tasks,
            "need_manual_processing": await ticket_count("manual_review"),
            "current_user_pending_tasks": current_user_tasks,
            "in_progress_tasks": in_progress_tasks,
            "all_in_progress_tasks": all_in_progress_tasks,
            "completed_exportable": await ticket_count("ready_for_export"),
            "resolved_manual_tasks": resolved_tasks,
            "need_customer_info": await ticket_count("need_customer_info"),
            "auto_replied": await ticket_count("auto_replied"),
            "error": await ticket_count("error"),
            "ready_for_export": await ticket_count("ready_for_export"),
            "ai_low_confidence": low_confidence,
            "recent_failed_jobs": [
                model_to_dict(
                    job,
                    (
                        "id",
                        "job_name",
                        "job_type",
                        "status",
                        "started_at",
                        "finished_at",
                        "processed_count",
                        "success_count",
                        "failed_count",
                        "error_message",
                    ),
                )
                for job in recent_failed_jobs
            ],
        }
    )
