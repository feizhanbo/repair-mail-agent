from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import imaplib
import logging
from datetime import date, timedelta
from email.message import EmailMessage
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_roles
from app.api.v1.deletions import raise_deletion_http
from app.config import settings
from app.core.database import get_session
from app.core.request_context import get_correlation_id
from app.core.response import ok, page
from app.core.email_classification import classification_catalog
from app.models import Email, EmailAttachment, JobRunLog, MailFetchRecord
from app.schemas.business import EmailIngestRequest, EmailReparseRequest
from app.services import emails as email_service
from app.services import deletions as deletion_service
from app.services import imap_fetcher
from app.services.audit import log_operation
from app.services.attachment_precheck import filter_decorative_attachments
from app.services.email_archival import EmailArchivalError, archive_email_bundle
from app.services.email_flow_trace import build_email_flow_trace
from app.services.email_preview import build_attachment_preview, build_email_preview
from app.services.eml import attachment_blobs_from_eml_bytes, payload_from_eml_bytes
from app.services.mail_precheck import precheck_email_payload
from app.services.common import utcnow
from app.services.master_data import EXCEL_MEDIA_TYPE, xlsx_bytes
from app.services.jobs import enqueue_job, recover_stale_jobs, serialize_job
from app.services.storage import StorageUploadError, generate_presigned_url_for_object

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


def _manual_attachment_blobs(payload: EmailIngestRequest) -> list[dict]:
    blobs: list[dict] = []
    for attachment in payload.attachments:
        content = _attachment_content_bytes(attachment)
        if content is None:
            raise ValueError("ATTACHMENT_CONTENT_REQUIRED")
        blobs.append(
            {
                "file_name": attachment.get("file_name") or "attachment",
                "content_type": attachment.get("content_type") or "application/octet-stream",
                "content": content,
                "is_inline": bool(attachment.get("is_inline")),
                "content_id": attachment.get("content_id"),
            }
        )
    return blobs


def _filter_attachment_blobs(payload: EmailIngestRequest, blobs: list[dict]) -> list[dict]:
    filtered, _ = filter_decorative_attachments(payload, blobs)
    return filtered


async def _archive_or_raise_http(
    session: AsyncSession,
    *,
    payload: EmailIngestRequest,
    raw_eml: bytes,
    raw_file_name: str,
    attachment_blobs: list[dict],
    source: str,
    user_id: int,
) -> None:
    try:
        await archive_email_bundle(
            session,
            payload=payload,
            raw_eml=raw_eml,
            raw_file_name=raw_file_name,
            attachment_blobs=attachment_blobs,
            source=source,
            user_id=user_id,
            correlation_id=get_correlation_id(),
        )
        # OSS metadata is durable before the formal email transaction starts.
        await session.commit()
    except EmailArchivalError as exc:
        await session.commit()
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        if exc.stage == "validate":
            http_status = (
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                if "TOO_LARGE" in exc.code or exc.code == "TOO_MANY_ATTACHMENTS"
                else status.HTTP_400_BAD_REQUEST
            )
        raise HTTPException(status_code=http_status, detail=exc.code) from exc


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
        target_id=precheck.duplicate_email_id,
        correlation_id=get_correlation_id(),
        email_id=precheck.duplicate_email_id,
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
    intent_subtype: str | None = None,
    handling_level: str | None = None,
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
        intent_subtype=intent_subtype,
        handling_level=handling_level,
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
    intent_subtype: str | None = None,
    handling_level: str | None = None,
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
        intent_subtype=intent_subtype,
        handling_level=handling_level,
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
        "intent_subtype",
        "handling_level",
        "classification_version",
        "classification_confidence",
        "classification_reason_code",
        "parse_status",
        "received_at",
        "attachment_count",
        "latest_parser_type",
        "latest_confidence_score",
        "latest_missing_fields",
        "latest_conflict_fields",
    ]
    return Response(
        content=await asyncio.to_thread(xlsx_bytes, rows, fieldnames),
        media_type=EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="emails-export.xlsx"'},
    )


@router.get("/classification-catalog")
async def get_classification_catalog(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(classification_catalog())


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
    except StorageUploadError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="OSS_OBJECT_NOT_READY") from exc
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
    except StorageUploadError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="OSS_OBJECT_NOT_READY") from exc
    return ok(
        {
            "attachment_id": attachment.id,
            "object_id": attachment.oss_object_id,
            "file_name": attachment.file_name,
            "url": url,
            "expires_seconds": expires_seconds,
        }
    )


@router.get("/attachments/{attachment_id}/preview")
async def attachment_preview(
    attachment_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await build_attachment_preview(session, attachment_id))


@router.get("/{email_id}/preview")
async def email_preview(
    email_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await build_email_preview(session, email_id))


@router.get("/fetch-status")
async def fetch_imap_status(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    if not ({"admin", "operator"} & set(current_user.roles)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")
    latest = await session.scalar(
        select(JobRunLog).where(JobRunLog.job_type == "imap_fetch").order_by(JobRunLog.created_at.desc()).limit(1)
    )
    active = await session.scalar(
        select(JobRunLog)
        .where(
            JobRunLog.job_type == "imap_fetch",
            or_(
                JobRunLog.status == "queued",
                (
                    (JobRunLog.status == "running")
                    & JobRunLog.locked_at.is_not(None)
                    & (JobRunLog.locked_at >= utcnow() - timedelta(seconds=settings.ASYNC_JOB_STALE_SECONDS))
                ),
                ((JobRunLog.status == "retry_wait") & (JobRunLog.attempt_count < JobRunLog.max_attempts)),
            ),
        )
        .order_by(JobRunLog.created_at.asc())
        .limit(1)
    )
    retry_count = int(
        await session.scalar(
            select(func.count()).select_from(MailFetchRecord).where(MailFetchRecord.fetch_status == "retry_wait")
        )
        or 0
    )
    return ok(
        {
            "enabled": settings.IMAP_FETCH_ENABLED,
            "configured": bool(settings.IMAP_HOST and settings.IMAP_USER and settings.IMAP_PASSWORD),
            "mailbox_account": settings.IMAP_USER,
            "folder": settings.IMAP_FOLDER,
            "poll_interval_minutes": settings.IMAP_POLL_INTERVAL_MINUTES,
            "fetch_limit": settings.IMAP_FETCH_LIMIT,
            "unseen_only": settings.IMAP_UNSEEN_ONLY,
            "read_only": True,
            "archive_to_oss": settings.IMAP_ARCHIVE_TO_OSS,
            "max_retries": settings.IMAP_MAX_RETRIES,
            "latest_job": serialize_job(latest) if latest else None,
            "active_job": serialize_job(active) if active else None,
            "retry_count": retry_count,
        }
    )


@router.post("/fetch/preflight")
async def preflight_imap_mailbox(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    folder_name: str | None = Query(default=None, min_length=1, max_length=255),
) -> dict:
    if "admin" not in current_user.roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")
    try:
        result = await imap_fetcher.preflight_imap(folder_name=folder_name or settings.IMAP_FOLDER)
    except imap_fetcher.ImapConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except imap_fetcher.ImapFetchError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except (OSError, TimeoutError, imaplib.IMAP4.error) as exc:
        logger.warning("IMAP preflight failed correlation_id=%s error=%s", get_correlation_id(), exc.__class__.__name__)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="IMAP_CONNECTION_FAILED") from exc
    return ok(result, "imap preflight passed")


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
        attachment_blobs = _manual_attachment_blobs(payload)
        attachment_blobs = _filter_attachment_blobs(payload, attachment_blobs)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ATTACHMENT_CONTENT_INVALID") from exc
    payload.raw_eml_sha256 = _sha256_bytes(raw_eml)
    precheck = await precheck_email_payload(session, payload)
    if not precheck.accepted:
        await _record_precheck_skip(session, user_id=current_user.id, source="manual_ingest", precheck=precheck)
        if precheck.status == "duplicate_message_skipped":
            result = await email_service.ingest_email(session, payload=payload, user_id=current_user.id)
        else:
            result = {"skipped": True, "precheck": precheck.to_dict()}
        await session.commit()
        return ok(result, "email skipped by precheck")

    await _archive_or_raise_http(
        session,
        payload=payload,
        raw_eml=raw_eml,
        raw_file_name="ingest.eml",
        attachment_blobs=attachment_blobs,
        source="manual_ingest",
        user_id=current_user.id,
    )
    result = await email_service.ingest_email(
        session,
        payload=payload,
        user_id=current_user.id,
        rule_analysis=precheck.rule_analysis,
    )
    await session.commit()
    return ok(result, "email ingested")


@router.post("/ingest/jobs")
async def ingest_email_job(
    payload: EmailIngestRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    try:
        raw_eml = _manual_raw_eml(payload)
        attachment_blobs = _manual_attachment_blobs(payload)
        attachment_blobs = _filter_attachment_blobs(payload, attachment_blobs)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ATTACHMENT_CONTENT_INVALID") from exc
    payload.raw_eml_sha256 = _sha256_bytes(raw_eml)
    precheck = await precheck_email_payload(session, payload)
    if not precheck.accepted:
        await _record_precheck_skip(session, user_id=current_user.id, source="manual_ingest_job", precheck=precheck)
        if precheck.status == "duplicate_message_skipped":
            result = await email_service.ingest_email(session, payload=payload, user_id=current_user.id, auto_parse=False)
        else:
            result = {"skipped": True, "precheck": precheck.to_dict()}
        await session.commit()
        return ok({"ingest": result, "job": None}, "email skipped by precheck")
    await _archive_or_raise_http(
        session,
        payload=payload,
        raw_eml=raw_eml,
        raw_file_name="ingest.eml",
        attachment_blobs=attachment_blobs,
        source="manual_ingest_job",
        user_id=current_user.id,
    )
    result = await email_service.ingest_email(
        session,
        payload=payload,
        user_id=current_user.id,
        auto_parse=False,
        rule_analysis=precheck.rule_analysis,
    )
    email_id = int(result["email"]["id"])
    job = await enqueue_job(
        session,
        job_type="email_parse",
        resource_type="email",
        resource_id=email_id,
        idempotency_key=f"email_parse:{email_id}:initial",
        metadata={
            "user_id": current_user.id,
            "reason": "initial asynchronous parse",
            "rule_parse_result_id": result["rule_parse_result_id"],
        },
    )
    await session.commit()
    return ok({"ingest": result, "job": serialize_job(job)}, "email archived and parse queued")


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
        blobs = attachment_blobs_from_eml_bytes(content)
        blobs = _filter_attachment_blobs(payload, blobs)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    precheck = await precheck_email_payload(session, payload)
    if not precheck.accepted:
        await _record_precheck_skip(session, user_id=current_user.id, source="eml_upload", precheck=precheck)
        if precheck.status == "duplicate_message_skipped":
            result = await email_service.ingest_email(session, payload=payload, user_id=current_user.id, auto_parse=auto_parse)
        else:
            result = {"skipped": True, "precheck": precheck.to_dict()}
        await session.commit()
        return ok(result, "eml skipped by precheck")

    await _archive_or_raise_http(
        session,
        payload=payload,
        raw_eml=content,
        raw_file_name=file.filename or "ingest.eml",
        attachment_blobs=blobs,
        source="eml_upload",
        user_id=current_user.id,
    )
    result = await email_service.ingest_email(
        session,
        payload=payload,
        user_id=current_user.id,
        auto_parse=auto_parse,
        rule_analysis=precheck.rule_analysis,
    )
    await session.commit()
    return ok(result, "eml ingested")


@router.post("/ingest-eml/jobs")
async def ingest_eml_job(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    file: UploadFile = File(...),
    mailbox_account: str = Form("manual-eml"),
    folder_name: str | None = Form("INBOX"),
) -> dict:
    if file.filename and not file.filename.lower().endswith(".eml"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EML_FILE_REQUIRED")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EML_FILE_EMPTY")
    try:
        payload = payload_from_eml_bytes(content, mailbox_account=mailbox_account, folder_name=folder_name)
        payload.raw_eml_sha256 = _sha256_bytes(content)
        blobs = attachment_blobs_from_eml_bytes(content)
        blobs = _filter_attachment_blobs(payload, blobs)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    precheck = await precheck_email_payload(session, payload)
    if not precheck.accepted:
        await _record_precheck_skip(session, user_id=current_user.id, source="eml_upload_job", precheck=precheck)
        if precheck.status == "duplicate_message_skipped":
            result = await email_service.ingest_email(session, payload=payload, user_id=current_user.id, auto_parse=False)
        else:
            result = {"skipped": True, "precheck": precheck.to_dict()}
        await session.commit()
        return ok({"ingest": result, "job": None}, "eml skipped by precheck")
    await _archive_or_raise_http(
        session,
        payload=payload,
        raw_eml=content,
        raw_file_name=file.filename or "ingest.eml",
        attachment_blobs=blobs,
        source="eml_upload_job",
        user_id=current_user.id,
    )
    result = await email_service.ingest_email(
        session,
        payload=payload,
        user_id=current_user.id,
        auto_parse=False,
        rule_analysis=precheck.rule_analysis,
    )
    email_id = int(result["email"]["id"])
    job = await enqueue_job(
        session,
        job_type="email_parse",
        resource_type="email",
        resource_id=email_id,
        idempotency_key=f"email_parse:{email_id}:initial",
        metadata={
            "user_id": current_user.id,
            "reason": "initial asynchronous EML parse",
            "rule_parse_result_id": result["rule_parse_result_id"],
        },
    )
    await session.commit()
    return ok({"ingest": result, "job": serialize_job(job)}, "eml archived and parse queued")


@router.post("/fetch-now", deprecated=True)
async def fetch_imap_now(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    folder_name: str | None = Query(default=None, min_length=1, max_length=255),
    limit: int | None = Query(default=None, ge=1, le=100),
    unseen_only: bool | None = Query(default=None),
    message_id: str | None = Query(default=None, max_length=500),
    auto_parse: bool = Query(True),
) -> dict:
    if "admin" not in current_user.roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")
    result = await imap_fetcher.run_imap_fetch_locked(
        session,
        folder_name=folder_name or settings.IMAP_FOLDER,
        limit=limit or settings.IMAP_FETCH_LIMIT,
        unseen_only=settings.IMAP_UNSEEN_ONLY if unseen_only is None else unseen_only,
        message_id=message_id,
        auto_parse=auto_parse,
        archive_to_oss=True,
        user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "imap fetched")


@router.post("/fetch/jobs")
async def fetch_imap_job(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    folder_name: str | None = Query(default=None, min_length=1, max_length=255),
    limit: int | None = Query(default=None, ge=1, le=100),
    unseen_only: bool | None = Query(default=None),
    message_id: str | None = Query(default=None, max_length=500),
    auto_parse: bool = Query(True),
) -> dict:
    if not ({"admin", "operator"} & set(current_user.roles)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")
    if await recover_stale_jobs(session):
        await session.commit()
    active_predicates = (
        JobRunLog.job_type == "imap_fetch",
        JobRunLog.status.in_(("queued", "running", "retry_wait")),
        or_(JobRunLog.status != "retry_wait", JobRunLog.attempt_count < JobRunLog.max_attempts),
    )
    active = await session.scalar(
        select(JobRunLog)
        .where(*active_predicates)
        .order_by(JobRunLog.created_at.asc())
        .limit(1)
    )
    if active is not None:
        return ok({"job": serialize_job(active), "reused": True}, "active imap fetch reused")
    # Serialize the second active check and commit so simultaneous clicks cannot
    # both observe an empty queue. The worker uses this same named lock.
    async with imap_fetcher.imap_fetch_lock() as acquired:
        if not acquired:
            active = await session.scalar(
                select(JobRunLog)
                .where(*active_predicates)
                .order_by(JobRunLog.created_at.asc())
                .limit(1)
            )
            if active is not None:
                return ok({"job": serialize_job(active), "reused": True}, "active imap fetch reused")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="IMAP_FETCH_BUSY")
        active = await session.scalar(
            select(JobRunLog)
            .where(*active_predicates)
            .order_by(JobRunLog.created_at.asc())
            .limit(1)
        )
        if active is not None:
            return ok({"job": serialize_job(active), "reused": True}, "active imap fetch reused")
        correlation_id = get_correlation_id() or "job"
        job = await enqueue_job(
            session,
            job_type="imap_fetch",
            resource_type="mailbox",
            resource_id=None,
            idempotency_key=f"imap_fetch:{correlation_id}",
            metadata={
                "folder_name": folder_name or settings.IMAP_FOLDER,
                "limit": limit or settings.IMAP_FETCH_LIMIT,
                "unseen_only": settings.IMAP_UNSEEN_ONLY if unseen_only is None else unseen_only,
                "message_id": message_id,
                "auto_parse": auto_parse,
                "user_id": current_user.id,
            },
        )
        await session.commit()
    return ok({"job": serialize_job(job), "reused": False}, "imap fetch queued")


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


@router.post("/{email_id}/reparse/jobs")
async def reparse_email_job(
    email_id: int,
    payload: EmailReparseRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    if await session.get(Email, email_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")
    correlation_id = get_correlation_id() or "job"
    job = await enqueue_job(
        session,
        job_type="email_reparse",
        resource_type="email",
        resource_id=email_id,
        idempotency_key=f"email_reparse:{email_id}:{correlation_id}",
        metadata={"user_id": current_user.id, "reason": payload.reason or "asynchronous reparse"},
    )
    await session.commit()
    return ok(serialize_job(job), "email reparse queued")


@router.get("/attachments/{attachment_id}/delete-preview")
async def attachment_delete_preview(
    attachment_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    try:
        return ok(await deletion_service.preview_attachment(session, attachment_id, current_user.id))
    except deletion_service.DeletionError as exc:
        raise_deletion_http(exc)


@router.delete("/attachments/{attachment_id}")
async def delete_attachment(
    attachment_id: int,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    reason: Annotated[str, Query(min_length=3, max_length=500)],
    confirmation_token: Annotated[str, Query(min_length=20)],
) -> dict:
    try:
        result = await deletion_service.delete_attachment(
            session,
            attachment_id=attachment_id,
            user_id=current_user.id,
            reason=reason,
            confirmation_token=confirmation_token,
        )
        if result["oss_status"] == "pending":
            response.status_code = status.HTTP_202_ACCEPTED
        return ok(result, "attachment deleted")
    except deletion_service.DeletionError as exc:
        raise_deletion_http(exc)


@router.get("/{email_id}/delete-preview")
async def email_delete_preview(
    email_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    try:
        return ok(await deletion_service.preview_email(session, email_id, current_user.id))
    except deletion_service.DeletionError as exc:
        raise_deletion_http(exc)


@router.delete("/{email_id}")
async def delete_email(
    email_id: int,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    reason: Annotated[str, Query(min_length=3, max_length=500)],
    confirmation_token: Annotated[str, Query(min_length=20)],
    force_local_cleanup: bool = False,
) -> dict:
    try:
        result = await deletion_service.delete_email(
            session,
            email_id=email_id,
            user_id=current_user.id,
            reason=reason,
            confirmation_token=confirmation_token,
            force_local_cleanup=force_local_cleanup,
        )
        if result["oss_status"] == "pending":
            response.status_code = status.HTTP_202_ACCEPTED
        return ok(result, "email deleted")
    except deletion_service.DeletionError as exc:
        raise_deletion_http(exc)
