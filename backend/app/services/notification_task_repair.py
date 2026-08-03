from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ManualReviewTask, NotificationEvent, NotificationUserState, RepairTicket, Role, User, UserRole
from app.services.audit import create_notification
from app.services.common import utcnow
from app.services.notifications import event_requires_attention, resolve_notifications_for_target
from app.services.routing import choose_available_operator


OPEN_LEGACY_STATUSES = ("pending", "assigned", "claimed", "assignment_failed")
TERMINAL_TASK_STATUSES = ("resolved", "closed")


async def _expected_recipients(session: AsyncSession, event: NotificationEvent) -> set[int]:
    statement = select(User.id).where(User.status == "active")
    if event.recipient_user_id is not None:
        statement = statement.where(User.id == event.recipient_user_id)
    else:
        statement = (
            statement.join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.role_code == (event.recipient_role_code or "operator"))
        )
    return set((await session.execute(statement)).scalars().all())


async def repair_notification_and_task_data(session: AsyncSession, *, apply: bool = False) -> dict[str, Any]:
    """Repair by business rules; dry-run is the mandatory default."""
    now = utcnow()
    counts: Counter[str] = Counter()
    samples: dict[str, list[int]] = {
        "missing_user_states": [],
        "resolved_notifications": [],
        "normalized_tasks": [],
        "assignment_failed_tasks": [],
    }
    active_operator_ids = set(
        (
            await session.execute(
                select(User.id)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(User.status == "active", Role.role_code == "operator")
            )
        ).scalars().all()
    )

    events = (await session.execute(select(NotificationEvent).order_by(NotificationEvent.id))).scalars().all()
    for event in events:
        expected_ticket_id = event.ticket_id
        if event.target_type in {"repair_ticket", "ticket"}:
            expected_ticket_id = event.target_id
        elif isinstance(event.metadata_json, dict) and isinstance(event.metadata_json.get("ticket_id"), int):
            expected_ticket_id = event.metadata_json["ticket_id"]
        if event.ticket_id != expected_ticket_id:
            counts["ticket_links_backfilled"] += 1
            if apply:
                event.ticket_id = expected_ticket_id
        expected_attention = event_requires_attention(event.event_type)
        if event.requires_attention != expected_attention:
            counts["attention_classification_backfilled"] += 1
            if apply:
                event.requires_attention = expected_attention
        existing_states = (
            await session.execute(
                select(NotificationUserState).where(NotificationUserState.notification_id == event.id)
            )
        ).scalars().all()
        existing_by_user = {state.user_id: state for state in existing_states}
        for user_id in await _expected_recipients(session, event):
            if user_id in existing_by_user:
                continue
            counts["missing_user_states"] += 1
            if len(samples["missing_user_states"]) < 20:
                samples["missing_user_states"].append(event.id)
            if apply:
                session.add(NotificationUserState(notification_id=event.id, user_id=user_id, status="unread"))
        for state in existing_states:
            if state.status == "read" and state.read_at is None:
                counts["read_at_backfilled"] += 1
                if apply:
                    state.read_at = event.read_at or event.delivered_at or event.created_at or now
            elif state.status == "resolved":
                if state.read_at is None:
                    counts["read_at_backfilled"] += 1
                    if apply:
                        state.read_at = event.read_at or event.delivered_at or event.created_at or now
                if state.resolved_at is None:
                    counts["resolved_at_backfilled"] += 1
                    if apply:
                        state.resolved_at = state.read_at or event.created_at or now

    tasks = (await session.execute(select(ManualReviewTask).order_by(ManualReviewTask.id))).scalars().all()
    for task in tasks:
        if task.status in TERMINAL_TASK_STATUSES:
            notification_ids = select(NotificationEvent.id).where(
                NotificationEvent.target_type == "manual_review_task",
                NotificationEvent.target_id == task.id,
            )
            states = (
                await session.execute(
                    select(NotificationUserState).where(
                        NotificationUserState.notification_id.in_(notification_ids),
                        NotificationUserState.status != "resolved",
                    )
                )
            ).scalars().all()
            for state in states:
                counts["resolved_notifications"] += 1
                if len(samples["resolved_notifications"]) < 20:
                    samples["resolved_notifications"].append(task.id)
                if apply:
                    state.status = "resolved"
                    state.read_at = state.read_at or task.resolved_at or now
                    state.resolved_at = state.resolved_at or task.resolved_at or now
            continue
        if task.status not in OPEN_LEGACY_STATUSES:
            continue
        ticket = await session.get(RepairTicket, task.ticket_id)
        owner_id = next(
            (
                candidate
                for candidate in (
                    task.assigned_user_id,
                    task.claimed_by_user_id,
                    ticket.assigned_user_id if ticket else None,
                )
                if candidate in active_operator_ids
            ),
            None,
        )
        if owner_id is None:
            fallback = await choose_available_operator(session)
            owner_id = fallback.id if fallback is not None else None
        new_status = "pending"
        if task.status != new_status or task.assigned_user_id != owner_id or task.claimed_by_user_id is not None or task.claimed_at is not None:
            counts["normalized_tasks"] += 1
            if len(samples["normalized_tasks"]) < 20:
                samples["normalized_tasks"].append(task.id)
            if owner_id is None:
                counts["assignment_failed_tasks"] += 1
                if len(samples["assignment_failed_tasks"]) < 20:
                    samples["assignment_failed_tasks"].append(task.id)
            if apply:
                task.status = new_status
                task.assigned_user_id = owner_id
                task.claimed_by_user_id = None
                task.claimed_at = None
                if owner_id is not None:
                    await resolve_notifications_for_target(
                        session,
                        target_type="manual_review_task",
                        target_id=task.id,
                    )
                    await create_notification(
                        session,
                        event_type="manual_review_assigned",
                        target_type="manual_review_task",
                        target_id=task.id,
                        title="人工复核任务已重新分配",
                        content=task.trigger_reason or "人工复核任务已分配给可用操作员。",
                        priority=task.priority,
                        recipient_user_id=owner_id,
                        metadata={"ticket_id": task.ticket_id, "task_type": task.task_type, "recovered": True},
                    )

    return {"mode": "apply" if apply else "dry_run", "counts": dict(counts), "samples": samples}
