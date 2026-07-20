from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok
from app.schemas.business import EmailThreadMergeRequest, EmailThreadSplitRequest
from app.services import emails as email_service

router = APIRouter()


@router.post("/{thread_id}/merge")
async def merge_thread(
    thread_id: int,
    payload: EmailThreadMergeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del thread_id, payload, session, current_user
    raise HTTPException(status_code=status.HTTP_410_GONE, detail="EMAIL_THREAD_MERGE_FORBIDDEN")


@router.post("/{thread_id}/split")
async def split_thread(
    thread_id: int,
    payload: EmailThreadSplitRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await email_service.split_thread(
        session,
        source_thread_id=thread_id,
        email_ids=payload.email_ids,
        user_id=current_user.id,
        reason=payload.reason,
    )
    await session.commit()
    return ok(result, "email thread split")
