from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ManualReviewTask
from app.services.audit import create_notification, log_operation
from app.services.common import model_to_dict, paginate_scalars, utcnow
from app.services.emails import reparse_email
from app.services.replies import create_reply_draft
from app.services.tickets import get_ticket, get_ticket_detail
from app.services.ticket_safety import validate_and_mark_ready_for_export
from app.services.notifications import resolve_notifications_for_target
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
    priority: str | None = None,
    assigned_user_id: int | None = None,
    current_user_id: int | None = None,
    scope: str | None = None,
    category: str | None = None,
    created_start: date | None = None,
    created_end: date | None = None,
) -> tuple[list[dict[str, Any]], int]:
    statement = select(ManualReviewTask)
    if task_status:
        statement = statement.where(ManualReviewTask.status == task_status)
    if task_type:
        statement = statement.where(ManualReviewTask.task_type == task_type)
    if category == "rma":
        statement = statement.where(
            ManualReviewTask.task_type.like("rma_%")
            | ManualReviewTask.task_type.in_(["warranty_status_unknown", "st_policy_expired"])
        )
    elif category == "sql":
        statement = statement.where(
            ManualReviewTask.task_type.like("sql_%")
            | ManualReviewTask.task_type.like("relay_%")
        )
    if priority:
        statement = statement.where(ManualReviewTask.priority == priority)
    if assigned_user_id:
        statement = statement.where(ManualReviewTask.assigned_user_id == assigned_user_id)
    if created_start:
        statement = statement.where(ManualReviewTask.created_at >= created_start)
    if created_end:
        end_exclusive = datetime.combine(created_end + timedelta(days=1), time.min, tzinfo=timezone.utc)
        statement = statement.where(ManualReviewTask.created_at < end_exclusive)
    if scope == "mine" and current_user_id:
        statement = statement.where(
            (ManualReviewTask.assigned_user_id == current_user_id)
            | (ManualReviewTask.claimed_by_user_id == current_user_id)
        )
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
    task.assigned_user_id = task.assigned_user_id or user_id
    ticket = await get_ticket(session, task.ticket_id)
    if ticket.assigned_user_id is None:
        ticket.assigned_user_id = user_id
    await log_operation(
        session,
        user_id=user_id,
        operation_type="manual_task_claimed",
        target_type="manual_review_task",
        target_id=task.id,
    )
    return serialize_task(task)


async def assign_task(
    session: AsyncSession,
    *,
    task_id: int,
    assigned_user_id: int | None,
    operator_user_id: int,
    reason: str | None = None,
) -> dict[str, Any]:
    task = await get_task(session, task_id)
    if task.status in {"resolved", "closed"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MANUAL_TASK_CLOSED")
    before = {
        "assigned_user_id": task.assigned_user_id,
        "claimed_by_user_id": task.claimed_by_user_id,
        "status": task.status,
    }
    task.assigned_user_id = assigned_user_id
    ticket = await get_ticket(session, task.ticket_id)
    ticket.assigned_user_id = assigned_user_id
    if assigned_user_id is None:
        task.status = "pending"
        task.claimed_by_user_id = None
        task.claimed_at = None
    elif task.status == "pending":
        task.status = "assigned"
    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="manual_task_assigned",
        target_type="manual_review_task",
        target_id=task.id,
        description=reason,
        before_data=before,
        after_data={"assigned_user_id": assigned_user_id, "status": task.status},
    )
    if assigned_user_id is not None:
        await create_notification(
            session,
            event_type="manual_review_assigned",
            target_type="manual_review_task",
            target_id=task.id,
            title="人工复核任务已分配",
            content=reason or task.trigger_reason or task.description,
            priority=task.priority,
            recipient_user_id=assigned_user_id,
            recipient_role_code=None,
            metadata={"ticket_id": task.ticket_id, "task_type": task.task_type},
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
    resolution_type: str | None = None,
    result_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = await get_task(session, task_id)
    if task.status in {"resolved", "closed"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MANUAL_TASK_ALREADY_RESOLVED")
    ticket = await get_ticket(session, task.ticket_id)
    followup_result: dict[str, Any] | None = None
    reparse_result: dict[str, Any] | None = None
    if next_action == "transition_ready_for_export":
        safety_result = await validate_and_mark_ready_for_export(session, ticket_id=ticket.id, user_id=user_id)
        if safety_result["status"] == "safety_failed":
            return {
                "task": serialize_task(task),
                "ticket": await get_ticket_detail(session, ticket.id),
                "safety_result": safety_result,
                "followup_result": None,
                "reparse_result": None,
            }
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
    elif next_action == "keep_manual_review":
        task.resolution = resolution
        await log_operation(
            session,
            user_id=user_id,
            operation_type="manual_task_kept_open",
            target_type="manual_review_task",
            target_id=task.id,
            description=resolution,
            after_data={"resolution_type": resolution_type, "result_payload": result_payload},
        )
        return {
            "task": serialize_task(task),
            "ticket": await get_ticket_detail(session, ticket.id),
            "followup_result": None,
            "reparse_result": None,
        }
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MANUAL_TASK_NEXT_ACTION_INVALID")

    task.status = "resolved"
    task.resolved_by_user_id = user_id
    task.resolved_at = utcnow()
    task.resolution = resolution

    await log_operation(
        session,
        user_id=user_id,
        operation_type="manual_task_resolved",
        target_type="manual_review_task",
        target_id=task.id,
        description=resolution,
        after_data={
            "resolution_type": resolution_type,
            "next_action": next_action,
            "result_payload": result_payload,
            "followup_reply_id": followup_result.get("reply", {}).get("id") if isinstance(followup_result, dict) else None,
            "reparse_result_id": (
                reparse_result.get("parse_result", {}).get("id")
                if isinstance(reparse_result, dict) and isinstance(reparse_result.get("parse_result"), dict)
                else None
            ),
        },
    )
    await resolve_notifications_for_target(
        session,
        target_type="manual_review_task",
        target_id=task.id,
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
