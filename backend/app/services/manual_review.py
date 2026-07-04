from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ManualReviewTask
from app.services.audit import log_operation
from app.services.common import model_to_dict, paginate_scalars, utcnow
from app.services.emails import reparse_email
from app.services.replies import create_reply_draft
from app.services.tickets import get_ticket, get_ticket_detail
from app.services.workflow import transition_ticket

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


def serialize_task(task: ManualReviewTask) -> dict[str, Any]:
    return model_to_dict(task, TASK_FIELDS)


async def get_task(session: AsyncSession, task_id: int) -> ManualReviewTask:
    task = await session.get(ManualReviewTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MANUAL_TASK_NOT_FOUND")
    return task


async def list_tasks(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    task_status: str | None = None,
    task_type: str | None = None,
    assigned_user_id: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    statement = select(ManualReviewTask)
    if task_status:
        statement = statement.where(ManualReviewTask.status == task_status)
    if task_type:
        statement = statement.where(ManualReviewTask.task_type == task_type)
    if assigned_user_id:
        statement = statement.where(ManualReviewTask.assigned_user_id == assigned_user_id)
    statement = statement.order_by(ManualReviewTask.created_at.desc(), ManualReviewTask.id.desc())
    tasks, total = await paginate_scalars(session, statement, page, page_size)
    return [serialize_task(task) for task in tasks], total


async def get_task_detail(session: AsyncSession, task_id: int) -> dict[str, Any]:
    task = await get_task(session, task_id)
    return {"task": serialize_task(task), "ticket_context": await get_ticket_detail(session, task.ticket_id)}


async def claim_task(session: AsyncSession, *, task_id: int, user_id: int) -> dict[str, Any]:
    task = await get_task(session, task_id)
    if task.status not in {"pending", "assigned"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MANUAL_TASK_NOT_CLAIMABLE")
    if task.assigned_user_id and task.assigned_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="MANUAL_TASK_ASSIGNED_TO_OTHER")
    task.status = "claimed"
    task.claimed_by_user_id = user_id
    task.claimed_at = utcnow()
    await log_operation(
        session,
        user_id=user_id,
        operation_type="manual_task_claimed",
        target_type="manual_review_task",
        target_id=task.id,
    )
    return serialize_task(task)


async def assign_task(session: AsyncSession, *, task_id: int, assigned_user_id: int, operator_user_id: int) -> dict[str, Any]:
    task = await get_task(session, task_id)
    if task.status in {"resolved", "closed"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MANUAL_TASK_CLOSED")
    task.assigned_user_id = assigned_user_id
    if task.status == "pending":
        task.status = "assigned"
    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="manual_task_assigned",
        target_type="manual_review_task",
        target_id=task.id,
        after_data={"assigned_user_id": assigned_user_id},
    )
    return serialize_task(task)


async def release_task(session: AsyncSession, *, task_id: int, user_id: int) -> dict[str, Any]:
    task = await get_task(session, task_id)
    if task.status not in {"claimed", "assigned"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MANUAL_TASK_NOT_RELEASABLE")
    if task.claimed_by_user_id and task.claimed_by_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="MANUAL_TASK_CLAIMED_BY_OTHER")
    task.status = "pending"
    task.claimed_by_user_id = None
    task.claimed_at = None
    await log_operation(
        session,
        user_id=user_id,
        operation_type="manual_task_released",
        target_type="manual_review_task",
        target_id=task.id,
    )
    return serialize_task(task)


async def resolve_task(
    session: AsyncSession,
    *,
    task_id: int,
    user_id: int,
    resolution: str,
    next_action: str,
) -> dict[str, Any]:
    task = await get_task(session, task_id)
    if task.status in {"resolved", "closed"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MANUAL_TASK_ALREADY_RESOLVED")
    ticket = await get_ticket(session, task.ticket_id)
    task.status = "resolved"
    task.resolved_by_user_id = user_id
    task.resolved_at = utcnow()
    task.resolution = resolution

    followup_result: dict[str, Any] | None = None
    reparse_result: dict[str, Any] | None = None
    if next_action == "transition_ready_for_export":
        if ticket.current_status_code == "manual_review":
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="ready_for_export",
                trigger_event="manual_resolved",
                user_id=user_id,
                reason=resolution,
            )
    elif next_action == "wait_customer_info":
        if ticket.current_status_code == "manual_review":
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="need_customer_info",
                trigger_event="manual_resolved",
                user_id=user_id,
                reason=resolution,
            )
        elif ticket.current_status_code == "parsed":
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="need_customer_info",
                trigger_event="missing_fields_detected",
                user_id=user_id,
                reason=resolution,
            )
    elif next_action == "generate_followup":
        if ticket.current_status_code == "manual_review":
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="need_customer_info",
                trigger_event="manual_resolved",
                user_id=user_id,
                reason=resolution,
            )
        followup_result = await create_reply_draft(session, ticket_id=ticket.id, user_id=user_id, related_email_id=task.email_id)
    elif next_action == "reparse":
        if task.email_id:
            reparse_result = await reparse_email(session, email_id=task.email_id, user_id=user_id, reason=resolution)
    elif next_action == "close_ticket":
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="closed",
            trigger_event="manual_close",
            user_id=user_id,
            reason=resolution,
        )
    elif next_action != "keep_manual_review":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MANUAL_TASK_NEXT_ACTION_INVALID")

    await log_operation(
        session,
        user_id=user_id,
        operation_type="manual_task_resolved",
        target_type="manual_review_task",
        target_id=task.id,
        description=resolution,
        after_data={"next_action": next_action},
    )
    return {
        "task": serialize_task(task),
        "ticket": await get_ticket_detail(session, ticket.id),
        "followup_result": followup_result,
        "reparse_result": reparse_result,
    }


async def reparse_task(
    session: AsyncSession,
    *,
    task_id: int,
    user_id: int,
    mode: str,
    reason: str | None = None,
) -> dict[str, Any]:
    task = await get_task(session, task_id)
    email_id = task.email_id
    if email_id is None:
        ticket = await get_ticket(session, task.ticket_id)
        email_id = ticket.source_email_id
    if email_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MANUAL_TASK_EMAIL_NOT_FOUND")

    reparse_result = await reparse_email(
        session,
        email_id=email_id,
        user_id=user_id,
        mode=mode,
        reason=reason or f"manual review task {task.id} triggered reparse",
    )
    await log_operation(
        session,
        user_id=user_id,
        operation_type="manual_task_reparsed",
        target_type="manual_review_task",
        target_id=task.id,
        description=reason,
        after_data={"email_id": email_id, "mode": mode},
    )
    return {
        "task": serialize_task(task),
        "ticket_context": await get_ticket_detail(session, task.ticket_id),
        "reparse_result": reparse_result,
    }
