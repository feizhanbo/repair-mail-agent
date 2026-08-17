from __future__ import annotations

import asyncio
import hashlib
import imaplib
import re
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import engine
from app.models import JobRunLog
from app.models.mail_fetch import MailFetchRecord
from app.services import emails as email_service
from app.services.attachment_precheck import filter_decorative_attachments
from app.services.email_archival import EmailArchivalError, archive_email_bundle
from app.services.common import normalize_message_id, utcnow
from app.services.eml import attachment_blobs_from_eml_bytes, payload_from_eml_bytes
from app.services.mail_precheck import precheck_email_payload, precheck_imap_uid
from app.services.jobs import enqueue_job
from app.services.logging_safety import safe_error_code


class ImapConfigurationError(RuntimeError):
    pass


class ImapFetchError(RuntimeError):
    pass


_mail_io_semaphore = asyncio.Semaphore(max(1, settings.MAIL_IO_CONCURRENCY))
_RETRY_MINUTES = (5, 15, 60, 180, 720)
_IMAP_LOCK_NAME = "repair_mail_agent_imap_fetch"


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


def _oss_configured() -> bool:
    return bool(settings.OSS_ENDPOINT and settings.OSS_BUCKET and settings.OSS_ACCESS_KEY and settings.OSS_SECRET_KEY)


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
        # Some IMAP servers tokenize HEADER searches and may return messages
        # whose References contains the requested id.  Narrow candidates by
        # parsing the actual Message-ID header before the ingestion loop.
        typ, data = client.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
    elif unseen_only:
        typ, data = client.uid("SEARCH", None, "UNSEEN", "NOT", "FROM", settings.IMAP_USER)
    else:
        typ, data = client.uid("SEARCH", None, "ALL", "NOT", "FROM", settings.IMAP_USER)
    if typ != "OK":
        raise ImapFetchError("IMAP_SEARCH_FAILED")
    raw = data[0] if data else b""
    uids = [_decode_uid(uid) for uid in raw.split() if uid]
    if message_id:
        exact: list[str] = []
        for uid in uids:
            fetched_type, fetched = client.uid(
                "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
            )
            if fetched_type != "OK":
                continue
            header = next(
                (
                    item[1]
                    for item in fetched or []
                    if isinstance(item, tuple) and isinstance(item[1], bytes)
                ),
                b"",
            )
            parsed = BytesParser(policy=policy.default).parsebytes(header)
            if normalize_message_id(str(parsed.get("Message-ID") or "")) == normalize_message_id(message_id):
                exact.append(uid)
        return exact
    return uids


def _uid_fetch_raw(client: imaplib.IMAP4_SSL, uid: str) -> bytes:
    typ, data = client.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data:
        raise ImapFetchError("IMAP_FETCH_FAILED")
    for item in data:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item[1]
    raise ImapFetchError("IMAP_FETCH_EMPTY")


def _uid_validity(client: imaplib.IMAP4_SSL) -> int:
    typ, data = client.response("UIDVALIDITY")
    if typ is None or not data:
        raise ImapFetchError("IMAP_UIDVALIDITY_MISSING")
    for item in data:
        value = item.decode("ascii", errors="ignore") if isinstance(item, bytes) else str(item)
        match = re.search(r"\d+", value)
        if match and int(match.group(0)) > 0:
            return int(match.group(0))
    raise ImapFetchError("IMAP_UIDVALIDITY_MISSING")


async def _select_readonly(client: imaplib.IMAP4_SSL, folder_name: str) -> int:
    typ, _ = await _mail_io(client.select, folder_name, readonly=True)
    if typ != "OK":
        raise ImapFetchError("IMAP_SELECT_FAILED")
    return await _mail_io(_uid_validity, client)


async def _find_fetch_record(
    session: AsyncSession,
    *,
    mailbox_account: str,
    folder_name: str,
    uid_validity: int,
    imap_uid: str,
) -> MailFetchRecord | None:
    return await session.scalar(
        select(MailFetchRecord).where(
            MailFetchRecord.mailbox_account == mailbox_account,
            MailFetchRecord.folder_name == folder_name,
            MailFetchRecord.uid_validity == uid_validity,
            MailFetchRecord.imap_uid == imap_uid,
        )
    )


async def _due_retry_uids(
    session: AsyncSession,
    *,
    folder_name: str,
    uid_validity: int,
) -> list[str]:
    rows = (
        await session.execute(
            select(MailFetchRecord.imap_uid).where(
                MailFetchRecord.mailbox_account == settings.IMAP_USER,
                MailFetchRecord.folder_name == folder_name,
                MailFetchRecord.uid_validity == uid_validity,
                MailFetchRecord.fetch_status.in_(("retry_wait", "failed")),
                MailFetchRecord.attempt_count < settings.IMAP_MAX_RETRIES,
                or_(
                    MailFetchRecord.next_retry_at.is_(None),
                    MailFetchRecord.next_retry_at <= utcnow(),
                ),
            )
        )
    ).scalars().all()
    return [str(uid) for uid in rows]


async def _save_fetch_result(
    session: AsyncSession,
    *,
    mailbox_account: str,
    folder_name: str,
    uid_validity: int,
    imap_uid: str,
    message_id: str,
    fetch_job_run_id: int | None,
    email_id: int | None,
    duplicate: bool,
    fetch_status: str,
    error_message: str | None = None,
) -> MailFetchRecord:
    record = await _find_fetch_record(
        session,
        mailbox_account=mailbox_account,
        folder_name=folder_name,
        uid_validity=uid_validity,
        imap_uid=imap_uid,
    )
    previous_attempts = int(record.attempt_count or 0) if record is not None else 0
    if record is None:
        record = MailFetchRecord(
            mailbox_account=mailbox_account,
            folder_name=folder_name,
            uid_validity=uid_validity,
            imap_uid=imap_uid,
            message_id=message_id,
        )
        session.add(record)
    record.message_id = message_id
    record.fetch_job_run_id = fetch_job_run_id
    record.email_id = email_id
    record.duplicate = duplicate
    record.fetch_status = fetch_status
    record.attempt_count = previous_attempts + 1
    record.last_attempt_at = utcnow()
    record.next_retry_at = None
    record.error_message = error_message
    return record


async def preflight_imap(*, folder_name: str = "INBOX") -> dict[str, Any]:
    """Validate the mailbox without searching, fetching, or changing message flags."""
    if not _oss_configured():
        raise ImapConfigurationError("OSS_NOT_CONFIGURED")
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = await _mail_io(_connect)
        uid_validity = await _select_readonly(client, folder_name)
        return {
            "status": "ready",
            "tls": True,
            "authenticated": True,
            "mailbox_account": settings.IMAP_USER,
            "folder": folder_name,
            "read_only": True,
            "uid_validity": uid_validity,
            "oss_configured": True,
            "messages_downloaded": 0,
            "flags_changed": False,
        }
    finally:
        if client is not None:
            try:
                await _mail_io(client.close)
            except Exception:
                pass
            try:
                await _mail_io(client.logout)
            except Exception:
                pass


@asynccontextmanager
async def imap_fetch_lock():
    """Hold a dedicated MySQL connection so commits in the work session cannot release ownership."""
    async with engine.connect() as connection:
        acquired = await connection.scalar(text("SELECT GET_LOCK(:lock_name, 0)"), {"lock_name": _IMAP_LOCK_NAME})
        try:
            yield acquired == 1
        finally:
            if acquired == 1:
                await connection.scalar(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": _IMAP_LOCK_NAME})


async def run_imap_fetch_locked(
    session: AsyncSession,
    *,
    busy_is_error: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    async with imap_fetch_lock() as acquired:
        if not acquired:
            if busy_is_error:
                raise ImapFetchError("IMAP_FETCH_BUSY")
            return {
                "status": "skipped_busy",
                "processed_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "fetched": [],
                "failures": [],
            }
        return await fetch_imap_emails(session, **kwargs)


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
    tracking_job: JobRunLog | None = None,
) -> dict[str, Any]:
    if not archive_to_oss:
        raise ImapConfigurationError("OSS_ARCHIVE_REQUIRED")
    if not _oss_configured():
        raise ImapConfigurationError("OSS_NOT_CONFIGURED")
    started = time.monotonic()
    job = tracking_job
    if job is None:
        job = JobRunLog(
            job_name="imap_fetch_now",
            job_type="imap_fetch",
            status="running",
            started_at=utcnow(),
            processed_count=0,
            success_count=0,
            failed_count=0,
            attempt_count=1,
            locked_at=utcnow(),
            locked_by="direct-imap-fetch",
            metadata_json={},
        )
        session.add(job)
        await session.flush()
    job.metadata_json = {
        **dict(job.metadata_json or {}),
        "folder_name": folder_name,
        "limit": limit,
        "unseen_only": unseen_only,
        "message_id": message_id,
        "archive_to_oss": archive_to_oss,
    }

    fetched: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped_count = 0
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = await _mail_io(_connect)
        uid_validity = await _select_readonly(client, folder_name)
        job.metadata_json = {**dict(job.metadata_json or {}), "uid_validity": uid_validity}
        uids = await _mail_io(_uid_search, client, message_id=message_id, unseen_only=unseen_only)
        retry_uid_set: set[str] = set()
        if message_id is None:
            retry_uids = await _due_retry_uids(session, folder_name=folder_name, uid_validity=uid_validity)
            retry_uid_set = set(retry_uids)
            for retry_uid in retry_uids:
                normalized_uid = str(retry_uid)
                if normalized_uid not in uids:
                    uids.append(normalized_uid)
        uids.sort(key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
        # The mailbox remains unread by design. Remove already handled/not-due
        # UIDs before applying the batch limit, otherwise the newest handled
        # UIDs can permanently starve older unprocessed mail.
        selected_uids: list[str] = []
        for uid in uids:
            uid_precheck = await precheck_imap_uid(
                session,
                mailbox_account=settings.IMAP_USER,
                folder_name=folder_name,
                uid_validity=uid_validity,
                imap_uid=uid,
            )
            if uid_precheck is not None:
                skipped_count += 1
                fetched.append({"uid": uid, "fetch_status": uid_precheck.status, "precheck": uid_precheck.to_dict()})
                continue
            selected_uids.append(uid)
            if limit > 0 and len(selected_uids) >= limit:
                break
        job.processed_count = len(selected_uids)

        for uid in selected_uids:
            job_id = int(job.id)
            previous_failed_count = int(job.failed_count or 0)
            try:
                raw = await _mail_io(_uid_fetch_raw, client, uid)
                payload = payload_from_eml_bytes(raw, mailbox_account=settings.IMAP_USER, folder_name=folder_name)
                payload.raw_eml_sha256 = hashlib.sha256(raw).hexdigest()
                payload.imap_uid = uid
                payload.fetch_job_run_id = job.id
                blobs = attachment_blobs_from_eml_bytes(raw)
                blobs, _attachment_precheck = filter_decorative_attachments(payload, blobs)
                payload_precheck = await precheck_email_payload(session, payload, enforce_target_mailbox=True)
                if not payload_precheck.accepted:
                    skipped_count += 1
                    await _save_fetch_result(
                        session,
                        mailbox_account=settings.IMAP_USER,
                        folder_name=folder_name,
                        uid_validity=uid_validity,
                        imap_uid=uid,
                        message_id=payload_precheck.message_id or payload.message_id or "",
                        fetch_job_run_id=job.id,
                        email_id=payload_precheck.duplicate_email_id,
                        duplicate=payload_precheck.status == "duplicate_message_skipped",
                        fetch_status=payload_precheck.status,
                        error_message=payload_precheck.reason[:1000],
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
                await _save_fetch_result(
                    session,
                    mailbox_account=settings.IMAP_USER,
                    folder_name=folder_name,
                    uid_validity=uid_validity,
                    imap_uid=uid,
                    message_id=payload.message_id,
                    fetch_job_run_id=job.id,
                    email_id=ingest_result.get("email", {}).get("id"),
                    duplicate=ingest_result.get("duplicate", False),
                    fetch_status="duplicate_message_skipped" if ingest_result.get("duplicate", False) else "ingested",
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
                job = await session.get(JobRunLog, job_id, with_for_update=True)
                if job is None:
                    raise RuntimeError("IMAP_JOB_NOT_FOUND_AFTER_ROLLBACK") from exc
                existing_failure = await _find_fetch_record(
                    session,
                    mailbox_account=settings.IMAP_USER,
                    folder_name=folder_name,
                    uid_validity=uid_validity,
                    imap_uid=uid,
                )
                attempt_count = (existing_failure.attempt_count if existing_failure else 0) + 1
                retry_index = min(attempt_count - 1, len(_RETRY_MINUTES) - 1)
                next_retry_at = utcnow() + timedelta(minutes=_RETRY_MINUTES[retry_index])
                if isinstance(exc, EmailArchivalError):
                    error_code = exc.code
                elif isinstance(exc, (ImapFetchError, ImapConfigurationError)):
                    error_code = str(exc) or exc.__class__.__name__
                else:
                    error_code = safe_error_code(exc, exc.__class__.__name__.upper()) or "IMAP_MESSAGE_FAILED"
                if uid in retry_uid_set and error_code in {"IMAP_FETCH_FAILED", "IMAP_FETCH_EMPTY"}:
                    error_code = "IMAP_UID_NOT_FOUND"
                if existing_failure is None:
                    existing_failure = MailFetchRecord(
                        mailbox_account=settings.IMAP_USER,
                        folder_name=folder_name,
                        uid_validity=uid_validity,
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
                job.failed_count = previous_failed_count + 1
                await _commit(session)
        job.status = "success" if not failures else "partial_success"
    except Exception as exc:
        job.status = "failed"
        job.error_code = safe_error_code(exc, "IMAP_FETCH_FAILED")
        job.error_message = exc.__class__.__name__
        await _commit(session)
        raise
    finally:
        job.finished_at = utcnow()
        job.duration_ms = int((time.monotonic() - started) * 1000)
        metadata = dict(job.metadata_json or {})
        metadata["skipped_count"] = skipped_count
        job.metadata_json = metadata
        job.locked_at = None
        job.locked_by = None
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
        "uid_validity": uid_validity,
        "fetched": fetched,
        "failures": failures,
    }
