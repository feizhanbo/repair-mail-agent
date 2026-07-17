from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotificationEvent, NotificationUserState
from app.services.common import model_to_dict, utcnow


NOTIFICATION_FIELDS = (
    "id",
    "event_type",
    "target_type",
    "target_id",
    "title",
    "content",
    "priority",
    "recipient_user_id",
    "recipient_role_code",
    "delivery_channel",
    "metadata_json",
    "delivered_at",
    "created_at",
)


def serialize_user_notification(event: NotificationEvent, state: NotificationUserState) -> dict:
    payload = model_to_dict(event, NOTIFICATION_FIELDS)
    payload.update(
        {
            "delivery_status": state.status,
            "read_at": state.read_at.isoformat() if state.read_at else None,
            "resolved_at": state.resolved_at.isoformat() if state.resolved_at else None,
        }
    )
    return payload


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
) -> None:
    now = utcnow()
    notification_ids = select(NotificationEvent.id).where(
        NotificationEvent.target_type == target_type,
        NotificationEvent.target_id == target_id,
    )
    await session.execute(
        update(NotificationUserState)
        .where(NotificationUserState.notification_id.in_(notification_ids), NotificationUserState.status != "resolved")
        .values(status="resolved", resolved_at=now, updated_at=now)
    )
