from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok, page
from app.models import NotificationEvent, NotificationUserState, User
from app.services.common import utcnow
from app.services.notifications import (
    list_user_notification_center,
    mark_user_notification_read,
    serialize_user_notification,
)

router = APIRouter()

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
    statement = (
        select(NotificationEvent, NotificationUserState, User)
        .join(NotificationUserState, NotificationUserState.notification_id == NotificationEvent.id)
        .join(User, User.id == NotificationUserState.user_id)
    )
    if "admin" not in current_user.roles:
        statement = statement.where(NotificationUserState.user_id == current_user.id)
    if delivery_status:
        normalized = "unread" if delivery_status == "pending" else delivery_status
        statement = statement.where(NotificationUserState.status == normalized)
    if event_type:
        statement = statement.where(NotificationEvent.event_type == event_type)
    if priority:
        statement = statement.where(NotificationEvent.priority == priority)
    if target_type:
        statement = statement.where(NotificationEvent.target_type == target_type)
    if created_start:
        statement = statement.where(NotificationEvent.created_at >= created_start)
    if created_end:
        end_exclusive = datetime.combine(created_end + timedelta(days=1), time.min, tzinfo=timezone.utc)
        statement = statement.where(NotificationEvent.created_at < end_exclusive)
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(or_(NotificationEvent.title.like(like), NotificationEvent.content.like(like)))
    statement = statement.order_by(NotificationEvent.created_at.desc(), NotificationEvent.id.desc())
    total = int(await session.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0)
    rows = (await session.execute(statement.offset((page_no - 1) * page_size).limit(page_size))).all()
    return page(
        [serialize_user_notification(event, state, owner) for event, state, owner in rows],
        total=total,
        page_no=page_no,
        page_size=page_size,
    )


@router.get("/center")
async def notification_center(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    priority: str | None = None,
    unread_only: bool = False,
) -> dict:
    center = await list_user_notification_center(
        session,
        user_id=None if "admin" in current_user.roles else current_user.id,
        page_no=page_no,
        page_size=page_size,
        priority=priority,
        unread_only=unread_only,
    )
    return ok(center)


@router.get("/center/summary")
async def notification_center_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    center = await list_user_notification_center(
        session,
        user_id=None if "admin" in current_user.roles else current_user.id,
        page_no=1,
        page_size=1,
    )
    return ok(
        {
            "attention_count": center["total"],
            "unread_attention_count": center["unread_total"],
        }
    )


@router.post("/center/{ticket_id}/read")
async def read_notification_center_group(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    now = utcnow()
    notification_ids = select(NotificationEvent.id).where(
        NotificationEvent.ticket_id == ticket_id,
        NotificationEvent.requires_attention.is_(True),
    )
    await session.execute(
        update(NotificationUserState)
        .where(
            NotificationUserState.user_id == current_user.id,
            NotificationUserState.notification_id.in_(notification_ids),
            NotificationUserState.status != "resolved",
        )
        .values(status="read", read_at=func.coalesce(NotificationUserState.read_at, now), updated_at=now)
    )
    await session.commit()
    return ok({"ticket_id": ticket_id}, "notification center group read")


@router.get("/unread-count")
async def unread_count(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    count = await session.scalar(
        select(func.count(NotificationUserState.id)).where(
            NotificationUserState.user_id == current_user.id,
            NotificationUserState.status == "unread",
        )
    )
    return ok({"count": int(count or 0)})


@router.post("/{notification_id}/read")
async def read_notification(
    notification_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    row = await mark_user_notification_read(
        session,
        notification_id=notification_id,
        user_id=current_user.id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOTIFICATION_NOT_FOUND")
    notification, user_state = row
    await session.commit()
    return ok(serialize_user_notification(notification, user_state), "notification read")


@router.get("/stream")
async def notification_stream(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> StreamingResponse:
    async def event_generator():
        payload = json.dumps({"type": "connected", "user_id": current_user.id}, ensure_ascii=False)
        yield f"event: connected\ndata: {payload}\n\n"
        await asyncio.sleep(0.1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
