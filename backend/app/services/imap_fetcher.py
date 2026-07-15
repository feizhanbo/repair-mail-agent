from __future__ import annotations

import asyncio
import hashlib
import imaplib
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import JobRunLog
from app.models.mail_fetch import MailFetchRecord
from app.services import emails as email_service
from app.services.attachment_precheck import filter_decorative_attachments
from app.services.email_archival import EmailArchivalError, archive_email_bundle
from app.services.common import utcnow
from app.services.eml import attachment_blobs_from_eml_bytes, payload_from_eml_bytes
from app.services.mail_precheck import precheck_email_payload, precheck_imap_uid
from app.services.jobs import enqueue_job


class ImapConfigurationError(RuntimeError):
    pass


class ImapFetchError(RuntimeError):
    pass


_mail_io_semaphore = asyncio.Semaphore(max(1, settings.MAIL_IO_CONCURRENCY))
_RETRY_MINUTES = (5, 15, 60, 180, 720)


async def _mail_io(func, *args, **kwargs):
    async with _mail_io_semaphore:
        return await asyncio.to_thread(func, *args, **kwargs)


async def _commit(session: AsyncSession) -> None:
    commit = getattr(session, "commit", None)
    if commit is not None:
        await commit()


async def _rollback(session: AsyncSession) -> None:
    rollback = getattr(session, "rollback", None)
    if rollback is not None:
        await rollback()


def _imap_configured() -> bool:
    return bool(settings.IMAP_HOST and settings.IMAP_USER and settings.IMAP_PASSWORD)


def _decode_uid(value: bytes | str) -> str:
    return value.decode("ascii", errors="ignore") if isinstance(value, bytes) else str(value)


def _connect() -> imaplib.IMAP4_SSL:
    if not _imap_configured():
        raise ImapConfigurationError("IMAP_NOT_CONFIGURED")
    client = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT, timeout=30)
    client.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
    return client


def _uid_search(client: imaplib.IMAP4_SSL, *, message_id: str | None, unseen_only: bool) -> list[str]:
    if message_id:
        typ, data = client.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
    elif unseen_only:
        typ, data = client.uid("SEARCH", None, "UNSEEN")
    else:
        typ, data = client.uid("SEARCH", None, "ALL")
    if typ != "OK":
        raise ImapFetchError("IMAP_SEARCH_FAILED")
    raw = data[0] if data else b""
    return [_decode_uid(uid) for uid in raw.split() if uid]


def _uid_fetch_raw(client: imaplib.IMAP4_SSL, uid: str) -> bytes:
    typ, data = client.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data:
        raise ImapFetchError("IMAP_FETCH_FAILED")
    for item in data:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item[1]
    raise ImapFetchError("IMAP_FETCH_EMPTY")


async def fetch_imap_emails(
    session: AsyncSession,
    *,
    folder_name: str = "INBOX",
    limit: int = 10,
    unseen_only: bool = True,
    message_id: str | None = None,
    auto_parse: bool = True,
    archive_to_oss: bool = True,
    user_id: int | None = None,
) -> dict[str, Any]:
    if not archive_to_oss:
        raise ImapConfigurationError("OSS_ARCHIVE_REQUIRED")
    started = time.monotonic()
    job = JobRunLog(
        job_name="imap_fetch_now",
        job_type="imap_fetch",
        status="running",
        started_at=utcnow(),
        processed_count=0,
        success_count=0,
        failed_count=0,
        metadata_json={
            "folder_name": folder_name,
            "limit": limit,
            "unseen_only": unseen_only,
            "message_id": message_id,
            "archive_to_oss": archive_to_oss,
        },
    )
    session.add(job)
    await session.flush()

    fetched: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped_count = 0
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = await _mail_io(_connect)
        typ, _ = await _mail_io(client.select, folder_name, readonly=True)
        if typ != "OK":
            raise ImapFetchError("IMAP_SELECT_FAILED")
        uids = await _mail_io(_uid_search, client, message_id=message_id, unseen_only=unseen_only)
        if limit > 0:
            uids = uids[-limit:]
        job.processed_count = len(uids)

        for uid in uids:
            try:
                uid_precheck = await precheck_imap_uid(
                    session,
                    mailbox_account=settings.IMAP_USER,
                    folder_name=folder_name,
                    imap_uid=uid,
                )
                if uid_precheck is not None:
                    skipped_count += 1
                    fetched.append({"uid": uid, "fetch_status": uid_precheck.status, "precheck": uid_precheck.to_dict()})
                    continue

                raw = await _mail_io(_uid_fetch_raw, client, uid)
                payload = payload_from_eml_bytes(raw, mailbox_account=settings.IMAP_USER, folder_name=folder_name)
                payload.raw_eml_sha256 = hashlib.sha256(raw).hexdigest()
                payload.imap_uid = uid
                payload.fetch_job_run_id = job.id
                blobs = attachment_blobs_from_eml_bytes(raw)
                blobs, _attachment_precheck = filter_decorative_attachments(payload, blobs)
                payload_precheck = await precheck_email_payload(session, payload)
                if not payload_precheck.accepted:
                    skipped_count += 1
                    session.add(
                        MailFetchRecord(
                            mailbox_account=settings.IMAP_USER,
                            folder_name=folder_name,
                            imap_uid=uid,
                            message_id=payload_precheck.message_id or payload.message_id or "",
                            fetch_job_run_id=job.id,
                            email_id=payload_precheck.duplicate_email_id,
                            duplicate=payload_precheck.status == "duplicate_message_skipped",
                            fetch_status=payload_precheck.status,
                            error_message=payload_precheck.reason[:1000],
                        )
                    )
                    fetched.append(
                        {
                            "uid": uid,
                            "message_id": payload_precheck.message_id,
                            "email_id": payload_precheck.duplicate_email_id,
                            "duplicate": payload_precheck.status == "duplicate_message_skipped",
                            "fetch_status": payload_precheck.status,
                            "precheck": payload_precheck.to_dict(),
                        }
                    )
                    await _commit(session)
                    continue

                try:
                    await archive_email_bundle(
                        session,
                        payload=payload,
                        raw_eml=raw,
                        raw_file_name=f"imap-{uid}.eml",
                        attachment_blobs=blobs,
                        source="imap",
                        user_id=user_id,
                        correlation_id=job.correlation_id,
                    )
                    await _commit(session)
                except EmailArchivalError:
                    # Preserve successful/failed OSS object metadata for an idempotent retry.
                    await _commit(session)
                    raise
                ingest_result = await email_service.ingest_email(
                    session,
                    payload=payload,
                    user_id=user_id,
                    auto_parse=False,
                    rule_analysis=payload_precheck.rule_analysis,
                )
                if auto_parse and not ingest_result.get("duplicate"):
                    email_id = int(ingest_result["email"]["id"])
                    await enqueue_job(
                        session,
                        job_type="email_parse",
                        resource_type="email",
                        resource_id=email_id,
                        idempotency_key=f"email_parse:{email_id}:initial",
                        correlation_id=job.correlation_id,
                        metadata={
                            "user_id": user_id,
                            "reason": "initial IMAP asynchronous parse",
                            "rule_parse_result_id": ingest_result["rule_parse_result_id"],
                        },
                    )
                session.add(
                    MailFetchRecord(
                        mailbox_account=settings.IMAP_USER,
                        folder_name=folder_name,
                        imap_uid=uid,
                        message_id=payload.message_id,
                        fetch_job_run_id=job.id,
                        email_id=ingest_result.get("email", {}).get("id"),
                        duplicate=ingest_result.get("duplicate", False),
                        fetch_status="duplicate_message_skipped" if ingest_result.get("duplicate", False) else "ingested",
                    )
                )
                fetched.append(
                    {
                        "uid": uid,
                        "message_id": payload.message_id,
                        "email_id": ingest_result.get("email", {}).get("id"),
                        "duplicate": ingest_result.get("duplicate", False),
                        "fetch_status": "duplicate_message_skipped" if ingest_result.get("duplicate", False) else "ingested",
                        "parse_status": ingest_result.get("email", {}).get("parse_status"),
                    }
                )
                job.success_count += 1
                await _commit(session)
            except Exception as exc:
                await _rollback(session)
                existing_failure = await session.scalar(
                    select(MailFetchRecord).where(
                        MailFetchRecord.mailbox_account == settings.IMAP_USER,
                        MailFetchRecord.folder_name == folder_name,
                        MailFetchRecord.imap_uid == uid,
                    )
                )
                attempt_count = (existing_failure.attempt_count if existing_failure else 0) + 1
                retry_index = min(attempt_count - 1, len(_RETRY_MINUTES) - 1)
                next_retry_at = utcnow() + timedelta(minutes=_RETRY_MINUTES[retry_index])
                error_code = exc.code if isinstance(exc, EmailArchivalError) else exc.__class__.__name__
                if existing_failure is None:
                    existing_failure = MailFetchRecord(
                        mailbox_account=settings.IMAP_USER,
                        folder_name=folder_name,
                        imap_uid=uid,
                        message_id="",
                        fetch_job_run_id=job.id,
                    )
                    session.add(existing_failure)
                existing_failure.fetch_status = "retry_wait" if attempt_count < settings.IMAP_MAX_RETRIES else "failed"
                existing_failure.attempt_count = attempt_count
                existing_failure.last_attempt_at = utcnow()
                existing_failure.next_retry_at = next_retry_at if attempt_count < settings.IMAP_MAX_RETRIES else None
                existing_failure.error_message = error_code
                failures.append({"uid": uid, "error": error_code})
                job.failed_count += 1
                await _commit(session)
        job.status = "success" if not failures else "partial_success"
    except Exception as exc:
        job.status = "failed"
        job.error_message = exc.__class__.__name__
        await _commit(session)
        raise
    finally:
        job.finished_at = utcnow()
        job.duration_ms = int((time.monotonic() - started) * 1000)
        metadata = dict(job.metadata_json or {})
        metadata["skipped_count"] = skipped_count
        job.metadata_json = metadata
        if client is not None:
            try:
                await _mail_io(client.close)
            except Exception:
                pass
            try:
                await _mail_io(client.logout)
            except Exception:
                pass

    return {
        "job_id": job.id,
        "status": job.status,
        "processed_count": job.processed_count,
        "success_count": job.success_count,
        "failed_count": job.failed_count,
        "skipped_count": skipped_count,
        "fetched": fetched,
        "failures": failures,
    }
