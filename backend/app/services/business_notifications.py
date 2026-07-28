from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotificationEvent, RepairTicket
from app.services.audit import create_notification


async def notify_ticket_once(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    event_type: str,
    title: str,
    content: str,
    priority: str = "normal",
    metadata: dict | None = None,
) -> NotificationEvent | None:
    existing = await session.scalar(
        select(NotificationEvent.id).where(
            NotificationEvent.event_type == event_type,
            NotificationEvent.target_type == "repair_ticket",
            NotificationEvent.target_id == ticket.id,
        )
    )
    if existing is not None:
        return None
    return await create_notification(
        session,
        event_type=event_type,
        target_type="repair_ticket",
        target_id=ticket.id,
        title=title,
        content=content,
        priority=priority,
        recipient_user_id=ticket.assigned_user_id,
        recipient_role_code=None if ticket.assigned_user_id else "operator",
        metadata={"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, **(metadata or {})},
    )
