from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from datetime import date
from email.message import EmailMessage
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok, page
from app.models import Email, EmailAttachment
from app.schemas.business import EmailIngestRequest, EmailReparseRequest
from app.services import emails as email_service
from app.services import imap_fetcher
from app.services.audit import log_operation
from app.services.email_flow_trace import build_email_flow_trace
from app.services.eml import attachment_blobs_from_eml_bytes, payload_from_eml_bytes
from app.services.mail_precheck import precheck_email_payload
from app.services.master_data import EXCEL_MEDIA_TYPE, xlsx_bytes
from app.services.storage import generate_presigned_url_for_object, upload_bytes_to_oss

logger = logging.getLogger(__name__)

router = APIRouter()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _attachment_content_bytes(attachment: dict) -> bytes | None:
    if attachment.get("content_base64"):
        return base64.b64decode(str(attachment["content_base64"]), validate=True)
    content = attachment.get("content")
    if content is None:
        return None
    if isinstance(content, bytes):
        return content
    return str(content).encode("utf-8")


def _manual_raw_eml(payload: EmailIngestRequest) -> bytes:
    msg = EmailMessage()
    msg["From"] = payload.from_address
    if payload.to_addresses:
        msg["To"] = payload.to_addresses
    if payload.cc_addresses:
        msg["Cc"] = payload.cc_addresses
    if payload.subject:
        msg["Subject"] = payload.subject
    if payload.message_id:
        msg["Message-ID"] = payload.message_id
    if payload.in_reply_to:
        msg["In-Reply-To"] = payload.in_reply_to
    if payload.references_header:
        msg["References"] = payload.references_header
    if payload.sent_at:
        from email.utils import format_datetime

        msg["Date"] = format_datetime(payload.sent_at)
    if payload.html_body:
        msg.set_content(payload.text_body or "")
        msg.add_alternative(payload.html_body, subtype="html")
    else:
        msg.set_content(payload.text_body or "", subtype="plain")
    for attachment in payload.attachments:
        content = _attachment_content_bytes(attachment)
        if content is None:
            continue
        content_type = str(attachment.get("content_type") or "application/octet-stream")
        maintype, _, subtype = content_type.partition("/")
        msg.add_attachment(
            content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.get("file_name") or "attachment",
        )
    return msg.as_bytes()


async def _record_precheck_skip(
    session: AsyncSession,
    *,
    user_id: int,
    source: str,
    precheck,
) -> None:
    await log_operation(
        session,
        user_id=user_id,
        operation_type="email_precheck_skipped",
        target_type="email",
        description=precheck.reason,
        after_data={"source": source, "precheck": precheck.to_dict()},
    )


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


@router.get("/export")
async def export_emails(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    parse_status: str | None = None,
    intent_type: str | None = None,
    keyword: str | None = None,
    subject: str | None = None,
    from_address: str | None = None,
    message_id: str | None = None,
    received_start: date | None = None,
    received_end: date | None = None,
) -> Response:
    del current_user
    rows = await email_service.export_emails(
        session,
        parse_status=parse_status,
        intent_type=intent_type,
        keyword=keyword,
        subject=subject,
        from_address=from_address,
        message_id=message_id,
        received_start=received_start,
        received_end=received_end,
    )
    fieldnames = [
        "id",
        "message_id",
        "subject",
        "from_address",
        "to_addresses",
        "intent_type",
        "parse_status",
        "received_at",
        "attachment_count",
        "latest_parser_type",
        "latest_confidence_score",
        "latest_missing_fields",
        "latest_conflict_fields",
    ]
    return Response(
        content=xlsx_bytes(rows, fieldnames),
        media_type=EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="emails-export.xlsx"'},
    )


@router.get("/{email_id}/flow-trace")
async def email_flow_trace(
    email_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await build_email_flow_trace(session, email_id))


@router.get("/{email_id}/raw-eml-url")
async def raw_eml_download_url(
    email_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    expires_seconds: int = Query(3600, ge=60, le=86400),
) -> dict:
    del current_user
    email = await session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")
    if not email.raw_eml_oss_object_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RAW_EML_NOT_ARCHIVED")
    try:
        url = await generate_presigned_url_for_object(session, oss_object_id=email.raw_eml_oss_object_id, expires_seconds=expires_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OSS_OBJECT_NOT_FOUND") from exc
    return ok(
        {
            "object_id": email.raw_eml_oss_object_id,
            "file_name": f"email-{email.id}.eml",
            "url": url,
            "expires_seconds": expires_seconds,
        }
    )


@router.get("/attachments/{attachment_id}/download-url")
async def attachment_download_url(
    attachment_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    expires_seconds: int = Query(3600, ge=60, le=86400),
) -> dict:
    del current_user
    attachment = await session.get(EmailAttachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ATTACHMENT_NOT_FOUND")
    if not attachment.oss_object_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ATTACHMENT_NOT_ARCHIVED")
    try:
        url = await generate_presigned_url_for_object(session, oss_object_id=attachment.oss_object_id, expires_seconds=expires_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OSS_OBJECT_NOT_FOUND") from exc
    return ok(
        {
            "attachment_id": attachment.id,
            "object_id": attachment.oss_object_id,
            "file_name": attachment.file_name,
            "url": url,
            "expires_seconds": expires_seconds,
        }
    )


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
    try:
        raw_eml = _manual_raw_eml(payload)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ATTACHMENT_CONTENT_INVALID") from exc
    payload.raw_eml_sha256 = _sha256_bytes(raw_eml)
    precheck = await precheck_email_payload(session, payload)
    if not precheck.accepted:
        if precheck.status == "duplicate_message_skipped":
            result = await email_service.ingest_email(session, payload=payload, user_id=current_user.id)
        else:
            await _record_precheck_skip(session, user_id=current_user.id, source="manual_ingest", precheck=precheck)
            result = {"skipped": True, "precheck": precheck.to_dict()}
        await session.commit()
        return ok(result, "email skipped by precheck")

    try:
        raw_object = await upload_bytes_to_oss(
            session,
            content=raw_eml,
            original_file_name="ingest.eml",
            content_type="message/rfc822",
            source_type="raw_eml",
            user_id=current_user.id,
        )
        payload.raw_eml_oss_object_id = raw_object.id
        for attachment in payload.attachments:
            attachment_content = _attachment_content_bytes(attachment)
            if attachment_content:
                attachment_object = await upload_bytes_to_oss(
                    session,
                    content=attachment_content,
                    original_file_name=attachment.get("file_name"),
                    content_type=attachment.get("content_type"),
                    source_type="email_attachment",
                    user_id=current_user.id,
                )
                attachment["oss_object_id"] = attachment_object.id
    except Exception:
        logger.exception("OSS archival failed for /ingest endpoint, continuing without archival")
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
        payload.raw_eml_sha256 = _sha256_bytes(content)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    precheck = await precheck_email_payload(session, payload)
    if not precheck.accepted:
        if precheck.status == "duplicate_message_skipped":
            result = await email_service.ingest_email(session, payload=payload, user_id=current_user.id, auto_parse=auto_parse)
        else:
            await _record_precheck_skip(session, user_id=current_user.id, source="eml_upload", precheck=precheck)
            result = {"skipped": True, "precheck": precheck.to_dict()}
        await session.commit()
        return ok(result, "eml skipped by precheck")

    try:
        raw_object = await upload_bytes_to_oss(
            session,
            content=content,
            original_file_name=file.filename or "ingest.eml",
            content_type="message/rfc822",
            source_type="raw_eml",
            user_id=current_user.id,
        )
        payload.raw_eml_oss_object_id = raw_object.id
        blobs = attachment_blobs_from_eml_bytes(content)
        for attachment, blob in zip(payload.attachments, blobs, strict=False):
            attachment_object = await upload_bytes_to_oss(
                session,
                content=blob["content"],
                original_file_name=blob["file_name"],
                content_type=blob.get("content_type"),
                source_type="email_attachment",
                user_id=current_user.id,
            )
            attachment["oss_object_id"] = attachment_object.id
    except Exception:
        logger.exception("OSS archival failed for /ingest-eml endpoint, continuing without archival")
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
    result = await email_service.reparse_email(session, email_id=email_id, user_id=current_user.id, reason=payload.reason)
    await session.commit()
    return ok(result, "email reparsed")
