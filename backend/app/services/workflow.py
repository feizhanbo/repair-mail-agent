from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ManualReviewTask, NotificationEvent, RepairTicket, TicketStatusLog, WorkflowStatus, WorkflowTransition
from app.services.audit import create_notification, log_operation
from app.services.common import utcnow

OPEN_TASK_STATUSES = ("pending", "claimed", "assigned")


def task_type_for_event(trigger_event: str) -> str:
    return {
        "parse_low_confidence": "parse_low_confidence",
        "field_conflict": "field_conflict",
        "manual_review_required": "manual",
        "system_error": "system_error",
        "followup_limit_exceeded": "followup_limit",
    }.get(trigger_event, "manual")


async def create_manual_task_if_missing(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    task_type: str,
    trigger_reason: str | None = None,
    priority: str = "normal",
    email_id: int | None = None,
    assigned_user_id: int | None = None,
) -> ManualReviewTask:
    result = await session.execute(
        select(ManualReviewTask)
        .where(
            ManualReviewTask.ticket_id == ticket.id,
            ManualReviewTask.task_type == task_type,
            ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
        )
        .order_by(ManualReviewTask.created_at.desc())
    )
    existing = result.scalars().first()
    if existing is not None:
        return existing

    sticky_assignee = assigned_user_id or ticket.assigned_user_id
    task = ManualReviewTask(
        ticket_id=ticket.id,
        email_id=email_id or ticket.source_email_id,
        task_type=task_type,
        priority=priority,
        status="pending",
        description=f"工单 {ticket.ticket_no} 需要人工复核。",
        trigger_reason=trigger_reason,
        assigned_user_id=sticky_assignee,
    )
    session.add(task)
    await session.flush()
    await create_notification(
        session,
        event_type="manual_review_created",
        target_type="manual_review_task",
        target_id=task.id,
        title="新的人工复核任务",
        content=trigger_reason or f"工单 {ticket.ticket_no} 需要人工处理。",
        priority=priority,
        recipient_user_id=None,
        recipient_role_code=None,
        metadata={"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "task_type": task_type},
    )
    if sticky_assignee:
        await create_notification(
            session,
            event_type="manual_review_assigned",
            target_type="manual_review_task",
            target_id=task.id,
            title="人工复核任务已分配给你",
            content=trigger_reason or f"工单 {ticket.ticket_no} 需要你处理。",
            priority=priority,
            recipient_user_id=sticky_assignee,
            recipient_role_code=None,
            metadata={"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "task_type": task_type},
        )
    return task


async def transition_ticket(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    to_status_code: str,
    trigger_event: str,
    user_id: int | None = None,
    operator_type: str = "user",
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RepairTicket:
    from_status_code = ticket.current_status_code
    if from_status_code == "closed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="TICKET_ALREADY_CLOSED")

    target_status = await session.scalar(
        select(WorkflowStatus).where(WorkflowStatus.status_code == to_status_code, WorkflowStatus.enabled == True)  # noqa: E712
    )
    if target_status is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="WORKFLOW_STATUS_NOT_FOUND")

    transition = await session.scalar(
        select(WorkflowTransition).where(
            WorkflowTransition.from_status_code == from_status_code,
            WorkflowTransition.to_status_code == to_status_code,
            WorkflowTransition.trigger_event == trigger_event,
            WorkflowTransition.enabled == True,  # noqa: E712
        )
    )
    if transition is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="WORKFLOW_TRANSITION_NOT_ALLOWED")

    ticket.current_status_code = to_status_code
    ticket.version += 1
    session.add(
        TicketStatusLog(
            ticket_id=ticket.id,
            from_status_code=from_status_code,
            to_status_code=to_status_code,
            trigger_event=trigger_event,
            reason=reason,
            operator_type=operator_type,
            operator_user_id=user_id,
            metadata_json=metadata,
        )
    )
    await log_operation(
        session,
        user_id=user_id,
        operation_type="ticket_transition",
        target_type="repair_ticket",
        target_id=ticket.id,
        description=reason,
        before_data={"status": from_status_code},
        after_data={"status": to_status_code, "trigger_event": trigger_event},
    )

    if to_status_code == "manual_review":
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type=task_type_for_event(trigger_event),
            trigger_reason=reason or transition.condition_desc,
            priority="high" if trigger_event in {"system_error", "field_conflict"} else "normal",
            email_id=ticket.source_email_id,
        )
    return ticket


async def mark_notification_read(session: AsyncSession, notification: NotificationEvent) -> NotificationEvent:
    notification.delivery_status = "read"
    notification.read_at = utcnow()
    notification.delivered_at = notification.delivered_at or notification.read_at
    return notification
