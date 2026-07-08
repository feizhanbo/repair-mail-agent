from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok, page
from app.schemas.business import EmailIngestRequest, EmailReparseRequest
from app.services import emails as email_service

router = APIRouter()


@router.get("")
async def list_emails(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    parse_status: str | None = None,
    intent_type: str | None = None,
    keyword: str | None = None,
    subject: str | None = None,
    from_address: str | None = None,
    message_id: str | None = None,
    received_start: date | None = None,
    received_end: date | None = None,
) -> dict:
    del current_user
    items, total = await email_service.list_emails(
        session,
        page=page_no,
        page_size=page_size,
        parse_status=parse_status,
        intent_type=intent_type,
        keyword=keyword,
        subject=subject,
        from_address=from_address,
        message_id=message_id,
        received_start=received_start,
        received_end=received_end,
    )
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.get("/{email_id}")
async def get_email(
    email_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await email_service.get_email_detail(session, email_id))


@router.post("/ingest")
async def ingest_email(
    payload: EmailIngestRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await email_service.ingest_email(session, payload=payload, user_id=current_user.id)
    await session.commit()
    return ok(result, "email ingested")


@router.post("/{email_id}/reparse")
async def reparse_email(
    email_id: int,
    payload: EmailReparseRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await email_service.reparse_email(session, email_id=email_id, user_id=current_user.id, mode=payload.mode, reason=payload.reason)
    await session.commit()
    return ok(result, "email reparsed")
