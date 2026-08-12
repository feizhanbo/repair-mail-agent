from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotificationEvent, NotificationUserState, RepairTicket, User
from app.services.common import model_to_dict, utcnow


NOTIFICATION_FIELDS = (
    "id",
    "event_type",
    "target_type",
    "target_id",
    "ticket_id",
    "title",
    "content",
    "priority",
    "recipient_user_id",
    "recipient_role_code",
    "delivery_channel",
    "metadata_json",
    "delivered_at",
    "created_at",
    "requires_attention",
)


# Notifications are deliberately classified in one place.  The event itself
# remains the audit/history record; this flag only controls whether it can be
# projected into the operational notification center.
ATTENTION_EVENT_TYPES = frozenset(
    {
        "manual_review_created",
        "manual_review_assigned",
        "manual_review_assignment_failed",
        "manual_review_owner_corrected",
        "ticket_customer_info_required",
        "ticket_system_error",
        "sap_export_failed",
        "sap_submit_uncertain",
        "sap_submit_unknown",
        "rma_reply_failed",
    }
)

def event_requires_attention(event_type: str) -> bool:
    """Return the center classification for an event type.

    Unknown events are intentionally history-only until they are explicitly
    classified.  This prevents a new informational event from unexpectedly
    becoming an operational alarm.
    """

    return event_type in ATTENTION_EVENT_TYPES


def serialize_user_notification(event: NotificationEvent, state: NotificationUserState, user: User | None = None) -> dict:
    payload = model_to_dict(event, NOTIFICATION_FIELDS)
    payload.update(
        {
            "delivery_status": state.status,
            "status": state.status,
            "read_at": state.read_at.isoformat() if state.read_at else None,
            "resolved_at": state.resolved_at.isoformat() if state.resolved_at else None,
            "state_user_id": state.user_id,
            "state_username": user.username if user is not None else None,
            "state_user_real_name": user.real_name if user is not None else None,
        }
    )
    return payload


def _center_group_key(event: NotificationEvent, user_id: int) -> str:
    if event.ticket_id is not None:
        return f"user:{user_id}:ticket:{event.ticket_id}"
    return f"user:{user_id}:{event.target_type}:{event.target_id}"


def _priority_rank(value: str | None) -> int:
    return {"high": 3, "normal": 2, "low": 1}.get(value or "normal", 0)


async def list_user_notification_center(
    session: AsyncSession,
    *,
    user_id: int | None,
    page_no: int = 1,
    page_size: int = 20,
    priority: str | None = None,
    unread_only: bool = False,
) -> dict[str, Any]:
    """Return one operational attention card per repair ticket.

    The query intentionally groups after applying the per-user state filter:
    two users can therefore see different cards/counts for the same event.
    """

    statement = (
        select(NotificationEvent, NotificationUserState, RepairTicket, User)
        .join(NotificationUserState, NotificationUserState.notification_id == NotificationEvent.id)
        .join(User, User.id == NotificationUserState.user_id)
        .outerjoin(RepairTicket, RepairTicket.id == NotificationEvent.ticket_id)
        .where(
            NotificationUserState.status != "resolved",
            NotificationEvent.requires_attention.is_(True),
        )
        .order_by(NotificationEvent.created_at.desc(), NotificationEvent.id.desc())
    )
    if user_id is not None:
        statement = statement.where(NotificationUserState.user_id == user_id)
    if priority:
        statement = statement.where(NotificationEvent.priority == priority)
    if unread_only:
        statement = statement.where(NotificationUserState.status == "unread")

    rows = (await session.execute(statement)).all()
    groups: dict[str, dict[str, Any]] = {}
    for event, state, ticket, owner in rows:
        key = _center_group_key(event, state.user_id)
        group = groups.setdefault(
            key,
            {
                "group_key": key,
                "ticket_id": event.ticket_id,
                "ticket_no": ticket.ticket_no if ticket is not None else None,
                "state_user_id": state.user_id,
                "state_username": owner.username,
                "state_user_real_name": owner.real_name,
                "active_event_count": 0,
                "unread_event_count": 0,
                "_representative": None,
            },
        )
        group["active_event_count"] += 1
        if state.status == "unread":
            group["unread_event_count"] += 1
        representative = group["_representative"]
        current_rank = (_priority_rank(event.priority), event.created_at, event.id)
        representative_rank = (
            (_priority_rank(representative.priority), representative.created_at, representative.id)
            if representative is not None
            else None
        )
        if representative is None or current_rank > representative_rank:
            group["_representative"] = event

    ordered = sorted(
        groups.values(),
        key=lambda item: (
            _priority_rank(item["_representative"].priority),
            item["_representative"].created_at,
            item["_representative"].id,
        ),
        reverse=True,
    )
    items: list[dict[str, Any]] = []
    for group in ordered[(page_no - 1) * page_size : page_no * page_size]:
        event = group.pop("_representative")
        group.update(
            {
                "title": event.title,
                "content": event.content,
                "event_type": event.event_type,
                "priority": event.priority,
                "latest_event_id": event.id,
                "latest_created_at": event.created_at.isoformat() if event.created_at else None,
                "target_type": event.target_type,
                "target_id": event.target_id,
                "status": "unread" if group["unread_event_count"] else "read",
            }
        )
        items.append(group)

    total = len(ordered)
    unread_groups = sum(1 for group in ordered if group["unread_event_count"])
    return {
        "items": items,
        "total": total,
        "unread_total": unread_groups,
        "page": page_no,
        "page_size": page_size,
    }


async def get_user_notification(
    session: AsyncSession,
    *,
    notification_id: int,
    user_id: int,
) -> tuple[NotificationEvent, NotificationUserState] | None:
    row = (
        await session.execute(
            select(NotificationEvent, NotificationUserState)
            .join(NotificationUserState, NotificationUserState.notification_id == NotificationEvent.id)
            .where(NotificationEvent.id == notification_id, NotificationUserState.user_id == user_id)
        )
    ).one_or_none()
    return row


async def mark_user_notification_read(
    session: AsyncSession,
    *,
    notification_id: int,
    user_id: int,
) -> tuple[NotificationEvent, NotificationUserState] | None:
    row = await get_user_notification(session, notification_id=notification_id, user_id=user_id)
    if row is None:
        return None
    event, state = row
    if state.status != "resolved":
        state.status = "read"
    state.read_at = state.read_at or utcnow()
    return event, state


async def resolve_notifications_for_target(
    session: AsyncSession,
    *,
    target_type: str,
    target_id: int,
    event_types: set[str] | None = None,
) -> None:
    now = utcnow()
    notification_ids = select(NotificationEvent.id).where(
        NotificationEvent.target_type == target_type,
        NotificationEvent.target_id == target_id,
    )
    if event_types:
        notification_ids = notification_ids.where(NotificationEvent.event_type.in_(event_types))
    await session.execute(
        update(NotificationUserState)
        .where(NotificationUserState.notification_id.in_(notification_ids), NotificationUserState.status != "resolved")
        .values(status="resolved", resolved_at=now, updated_at=now)
    )


async def resolve_notifications_for_ticket(
    session: AsyncSession,
    *,
    ticket_id: int,
    event_types: set[str] | None = None,
) -> None:
    """Resolve only actionable notifications belonging to a ticket.

    Informational success messages remain visible in the station inbox and do
    not get marked resolved merely because a ticket reached a later state.
    """

    now = utcnow()
    statement = select(NotificationEvent.id).where(
        NotificationEvent.ticket_id == ticket_id,
        NotificationEvent.requires_attention.is_(True),
    )
    if event_types:
        statement = statement.where(NotificationEvent.event_type.in_(event_types))
    await session.execute(
        update(NotificationUserState)
        .where(
            NotificationUserState.notification_id.in_(statement),
            NotificationUserState.status != "resolved",
        )
        .values(status="resolved", resolved_at=now, updated_at=now)
    )
