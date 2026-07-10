from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok, page
from app.models import NotificationEvent
from app.services.common import model_to_dict, paginate_scalars
from app.services.workflow import mark_notification_read

router = APIRouter()

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
    "delivery_status",
    "read_at",
    "metadata_json",
    "delivered_at",
    "created_at",
)


@router.get("")
async def list_notifications(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    delivery_status: str | None = None,
    event_type: str | None = None,
    priority: str | None = None,
    target_type: str | None = None,
    keyword: str | None = None,
    created_start: date | None = None,
    created_end: date | None = None,
) -> dict:
    statement = select(NotificationEvent).where(
        or_(
            NotificationEvent.recipient_user_id == current_user.id,
            NotificationEvent.recipient_role_code.in_(current_user.roles),
            and_(NotificationEvent.recipient_user_id.is_(None), NotificationEvent.recipient_role_code.is_(None)),
        )
    )
    if delivery_status:
        statement = statement.where(NotificationEvent.delivery_status == delivery_status)
    if event_type:
        statement = statement.where(NotificationEvent.event_type == event_type)
    if priority:
        statement = statement.where(NotificationEvent.priority == priority)
    if target_type:
        statement = statement.where(NotificationEvent.target_type == target_type)
    if created_start:
        statement = statement.where(NotificationEvent.created_at >= created_start)
    if created_end:
        statement = statement.where(NotificationEvent.created_at <= created_end)
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(or_(NotificationEvent.title.like(like), NotificationEvent.content.like(like)))
    statement = statement.order_by(NotificationEvent.created_at.desc(), NotificationEvent.id.desc())
    rows, total = await paginate_scalars(session, statement, page_no, page_size)
    return page([model_to_dict(row, NOTIFICATION_FIELDS) for row in rows], total=total, page_no=page_no, page_size=page_size)


@router.post("/{notification_id}/read")
async def read_notification(
    notification_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    notification = await session.get(NotificationEvent, notification_id)
    if notification is None:
        return ok({}, "notification not found")
    if notification.recipient_user_id not in (None, current_user.id) and notification.recipient_role_code not in current_user.roles:
        return ok({}, "notification not found")
    await mark_notification_read(session, notification)
    payload = model_to_dict(notification, NOTIFICATION_FIELDS)
    await session.commit()
    return ok(payload, "notification read")


@router.get("/stream")
async def notification_stream(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> StreamingResponse:
    async def event_generator():
        payload = json.dumps({"type": "connected", "user_id": current_user.id}, ensure_ascii=False)
        yield f"event: connected\ndata: {payload}\n\n"
        await asyncio.sleep(0.1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
