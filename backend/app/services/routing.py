from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Email, ManualReviewTask, OperationLog, RepairTicket, Role, User, UserRole
from app.services.audit import create_notification, log_operation
from app.services.notifications import resolve_notifications_for_target


CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
OPEN_TASK_STATUSES = ("pending", "claimed", "assigned", "assignment_failed")
SCOPE_OWNER_USERNAMES = {"domestic": "miya", "overseas": "demi"}


def detect_language(email: Email) -> str:
    sample = "\n".join(
        value
        for value in (email.subject, email.latest_reply_segment, email.clean_body, email.text_body)
        if value
    )[:12000]
    if CJK_PATTERN.search(sample):
        return "zh-CN"
    if re.search(r"[A-Za-z]", sample):
        return "en-US"
    return "unknown"


async def choose_system_owner(session: AsyncSession, email: Email) -> tuple[int | None, str, str]:
    del session
    language = detect_language(email)
    return None, language, f"language:{language};customer_scope:unresolved;owner:unassigned"


def _operator_query():
    return (
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(User.status == "active", Role.role_code == "operator")
    )


async def choose_scope_owner(session: AsyncSession, customer_scope: str | None) -> User | None:
    username = SCOPE_OWNER_USERNAMES.get(str(customer_scope or ""))
    if username is None:
        return None
    return await session.scalar(_operator_query().where(User.username == username))


async def route_ticket_by_customer_scope(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    user_id: int | None = None,
) -> dict[str, object]:
    owner = await choose_scope_owner(session, ticket.customer_scope)
    if owner is None:
        await create_notification(
            session,
            event_type="manual_review_assignment_failed",
            target_type="repair_ticket",
            target_id=ticket.id,
            title="客户范围负责人不可用",
            content=f"工单 {ticket.ticket_no} 的 customer_scope={ticket.customer_scope!r} 无可用指定操作员。",
            priority="high",
            recipient_user_id=None,
            recipient_role_code="admin",
            metadata={"ticket_id": ticket.id, "customer_scope": ticket.customer_scope},
        )
        return {"status": "unassigned", "customer_scope": ticket.customer_scope, "owner_id": None}

    ticket_manually_assigned = bool(await session.scalar(
        select(OperationLog.id).where(
            OperationLog.target_type == "repair_ticket",
            OperationLog.target_id == ticket.id,
            OperationLog.operation_type == "ticket_owner_corrected",
        ).limit(1)
    ))
    if not ticket_manually_assigned:
        ticket.assigned_user_id = owner.id

    tasks = list((await session.execute(
        select(ManualReviewTask).where(
            ManualReviewTask.ticket_id == ticket.id,
            ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
        )
    )).scalars().all())
    routed_task_ids: list[int] = []
    for task in tasks:
        if task.claimed_by_user_id is not None or task.status == "claimed":
            continue
        manually_assigned = bool(await session.scalar(
            select(OperationLog.id).where(
                OperationLog.target_type == "manual_review_task",
                OperationLog.target_id == task.id,
                OperationLog.operation_type == "manual_task_assigned",
            ).limit(1)
        ))
        if manually_assigned:
            continue
        await resolve_notifications_for_target(session, target_type="manual_review_task", target_id=task.id)
        task.assigned_user_id = owner.id
        task.status = "pending"
        routed_task_ids.append(task.id)
        await create_notification(
            session,
            event_type="manual_review_assigned",
            target_type="manual_review_task",
            target_id=task.id,
            title="人工复核任务已按客户范围分配",
            content=f"工单 {ticket.ticket_no} 已按 {ticket.customer_scope} 分配。",
            priority=task.priority,
            recipient_user_id=owner.id,
            recipient_role_code=None,
            metadata={"ticket_id": ticket.id, "customer_scope": ticket.customer_scope},
        )

    await log_operation(
        session,
        user_id=user_id,
        operation_type="ticket_owner_routed_by_customer_scope",
        target_type="repair_ticket",
        target_id=ticket.id,
        ticket_id=ticket.id,
        description="按客户范围分配工单及未受人工保护的开放任务。",
        after_data={
            "customer_scope": ticket.customer_scope,
            "owner_user_id": ticket.assigned_user_id,
            "scope_owner_user_id": owner.id,
            "ticket_manual_override_preserved": ticket_manually_assigned,
            "routed_task_ids": routed_task_ids,
        },
    )
    return {
        "status": "routed",
        "customer_scope": ticket.customer_scope,
        "owner_id": ticket.assigned_user_id,
        "scope_owner_id": owner.id,
        "routed_task_ids": routed_task_ids,
    }

async def choose_available_operator(
    session: AsyncSession,
    preferred_user_id: int | None = None,
    *,
    allow_fallback: bool = True,
) -> User | None:
    """Return a valid preferred operator, otherwise the least-loaded active operator."""
    base = _operator_query()
    if preferred_user_id is not None:
        preferred = await session.scalar(base.where(User.id == preferred_user_id))
        if preferred is not None:
            return preferred
        if not allow_fallback:
            return None

    if not allow_fallback:
        return None

    open_count = (
        select(func.count(ManualReviewTask.id))
        .where(
            ManualReviewTask.assigned_user_id == User.id,
            ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
        )
        .correlate(User)
        .scalar_subquery()
    )
    return await session.scalar(base.order_by(open_count.asc(), User.id.asc()).limit(1))
