from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok, page
from app.schemas.business import EmailIngestRequest, EmailReparseRequest
from app.services import emails as email_service
from app.services import imap_fetcher
from app.services.email_flow_trace import build_email_flow_trace
from app.services.eml import payload_from_eml_bytes

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


@router.get("/{email_id}/flow-trace")
async def get_email_flow_trace(
    email_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await build_email_flow_trace(session, email_id=email_id))


@router.post("/ingest")
async def ingest_email(
    payload: EmailIngestRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await email_service.ingest_email(session, payload=payload, user_id=current_user.id)
    await session.commit()
    return ok(result, "email ingested")


@router.post("/ingest-eml")
async def ingest_eml(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    file: UploadFile = File(...),
    mailbox_account: str = Form("manual-eml"),
    folder_name: str | None = Form("INBOX"),
    auto_parse: bool = Form(True),
) -> dict:
    if file.filename and not file.filename.lower().endswith(".eml"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EML_FILE_REQUIRED")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EML_FILE_EMPTY")
    try:
        payload = payload_from_eml_bytes(content, mailbox_account=mailbox_account, folder_name=folder_name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    result = await email_service.ingest_email(session, payload=payload, user_id=current_user.id, auto_parse=auto_parse)
    await session.commit()
    return ok(result, "eml ingested")


@router.post("/fetch-now")
async def fetch_imap_now(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    folder_name: str = Query("INBOX", min_length=1, max_length=255),
    limit: int = Query(10, ge=1, le=100),
    unseen_only: bool = Query(True),
    message_id: str | None = Query(default=None, max_length=500),
    auto_parse: bool = Query(True),
    archive_to_oss: bool = Query(True),
) -> dict:
    if not ({"admin", "supervisor"} & set(current_user.roles)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")
    result = await imap_fetcher.fetch_imap_emails(
        session,
        folder_name=folder_name,
        limit=limit,
        unseen_only=unseen_only,
        message_id=message_id,
        auto_parse=auto_parse,
        archive_to_oss=archive_to_oss,
        user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "imap fetched")


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
