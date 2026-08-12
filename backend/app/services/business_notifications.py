from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotificationEvent, RepairTicket
from app.services.audit import create_notification
from app.services.notifications import resolve_notifications_for_ticket


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
    # A later successful business event closes the earlier actionable failure,
    # while both records remain available in the station-message history.
    if event_type in {"sap_export_accepted", "rma_reply_sent"}:
        await resolve_notifications_for_ticket(
            session,
            ticket_id=ticket.id,
            event_types={
                "sap_export_failed",
                "sap_submit_uncertain",
                "sap_submit_unknown",
                "rma_reply_failed",
            },
        )
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
        ticket_id=ticket.id,
        recipient_user_id=ticket.assigned_user_id,
        recipient_role_code=None if ticket.assigned_user_id else "operator",
        metadata={"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, **(metadata or {})},
    )
