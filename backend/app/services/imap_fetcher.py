from __future__ import annotations

import asyncio
import hashlib
import imaplib
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import JobRunLog, MailboxSyncState, ManualReviewTask
from app.models.mail_fetch import MailFetchRecord
from app.services.attachment_precheck import filter_decorative_attachments
from app.services.email_archival import EmailArchivalError, archive_raw_email
from app.services.jobs import enqueue_job
from app.services.common import normalize_message_id, utcnow
from app.services.eml import attachment_blobs_from_eml_bytes, payload_from_eml_bytes
from app.services.mail_precheck import precheck_email_payload, precheck_imap_uid
from app.services.mailbox_sync import (
    MailboxSyncConfigurationError,
    apply_uid_validity,
    get_or_create_sync_state,
    imap_since_date,
    mark_initial_complete,
    record_discovery,
)
from app.services.logging_safety import safe_error_code


class ImapConfigurationError(RuntimeError):
    pass


class ImapFetchError(RuntimeError):
    pass


_mail_io_semaphore = asyncio.Semaphore(max(1, settings.MAIL_IO_CONCURRENCY))
_RETRY_MINUTES = (5, 15, 60, 180, 720)
logger = logging.getLogger(__name__)


def _imap_lock_name(folder_name: str) -> str:
    identity = f"{settings.IMAP_USER.lower()}:{folder_name}"
    return f"repair_mail_imap:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"


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


def _uid_search(
    client: imaplib.IMAP4_SSL,
    *,
    message_id: str | None,
    unseen_only: bool = False,
    start_uid: int | None = None,
    since_date: str | None = None,
) -> list[str]:
    if message_id:
        # Some IMAP servers tokenize HEADER searches and may return messages
        # whose References contains the requested id.  Narrow candidates by
        # parsing the actual Message-ID header before the ingestion loop.
        typ, data = client.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
    elif start_uid is not None:
        typ, data = client.uid("SEARCH", None, "UID", f"{max(1, start_uid)}:*", "NOT", "FROM", settings.IMAP_USER)
    elif since_date:
        typ, data = client.uid("SEARCH", None, "SINCE", since_date, "NOT", "FROM", settings.IMAP_USER)
    else:
        raise ImapConfigurationError("IMAP_SYNC_BOUNDARY_REQUIRED")
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


def _uid_fetch_internal_date(client: imaplib.IMAP4_SSL, uid: str) -> datetime | None:
    typ, data = client.uid("FETCH", uid, "(INTERNALDATE)")
    if typ != "OK" or not data:
        return None
    for item in data:
        metadata = item[0] if isinstance(item, tuple) else item
        if not isinstance(metadata, bytes):
            continue
        match = re.search(rb'INTERNALDATE\s+"([^"]+)"', metadata, flags=re.IGNORECASE)
        if match is None:
            continue
        try:
            return datetime.strptime(match.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
        except (UnicodeDecodeError, ValueError):
            logger.warning("Invalid IMAP INTERNALDATE", extra={"event": "imap_internaldate_invalid", "imap_uid": uid})
    return None


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


async def _pending_discovered_uids(
    session: AsyncSession,
    *,
    folder_name: str,
    uid_validity: int,
    limit: int,
) -> list[str]:
    rows = (
        await session.execute(
            select(MailFetchRecord.imap_uid).where(
                MailFetchRecord.mailbox_account == settings.IMAP_USER,
                MailFetchRecord.folder_name == folder_name,
                MailFetchRecord.uid_validity == uid_validity,
                MailFetchRecord.fetch_status == "discovered",
            ).order_by(MailFetchRecord.imap_uid.asc()).limit(max(1, limit))
        )
    ).scalars().all()
    return [str(uid) for uid in rows]


async def _discover_new_uids(
    session: AsyncSession,
    *,
    state: MailboxSyncState,
    folder_name: str,
    uid_validity: int,
    candidate_uids: list[str],
    limit: int,
) -> list[str]:
    if not candidate_uids or limit <= 0:
        return []
    existing = {
        str(row.imap_uid): row
        for row in (
            await session.execute(
                select(MailFetchRecord).where(
                    MailFetchRecord.mailbox_account == settings.IMAP_USER,
                    MailFetchRecord.folder_name == folder_name,
                    MailFetchRecord.uid_validity == uid_validity,
                    MailFetchRecord.imap_uid.in_(candidate_uids),
                )
            )
        ).scalars().all()
    }
    discovered: list[str] = []
    inspected: list[str] = []
    for uid in candidate_uids:
        inspected.append(uid)
        record = existing.get(uid)
        if record is None:
            session.add(
                MailFetchRecord(
                    mailbox_account=settings.IMAP_USER,
                    folder_name=folder_name,
                    uid_validity=uid_validity,
                    imap_uid=uid,
                    message_id=None,
                    fetch_status="discovered",
                    processing_stage="discovered",
                    attempt_count=0,
                )
            )
            discovered.append(uid)
        elif record.fetch_status == "discovered":
            discovered.append(uid)
        if len(discovered) >= limit:
            break
    record_discovery(state, inspected)
    return discovered


async def _save_fetch_result(
    session: AsyncSession,
    *,
    mailbox_account: str,
    folder_name: str,
    uid_validity: int,
    imap_uid: str,
    message_id: str | None,
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
    record.attempt_count = (
        previous_attempts + 1
        if record.id is not None and fetch_status in {"processing", "spooled"}
        else max(1, previous_attempts)
    )
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


async def sent_folder_contains_message(message_id: str) -> bool:
    """Use exact Message-ID lookup as evidence for an uncertain SMTP result."""
    if not message_id or not settings.SMTP_SENT_FOLDER or not _imap_configured():
        return False
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = await _mail_io(_connect)
        typ, _ = await _mail_io(client.select, settings.SMTP_SENT_FOLDER, True)
        if typ != "OK":
            return False
        matches = await _mail_io(_uid_search, client, message_id=message_id)
        return bool(matches)
    except Exception:
        logger.warning(
            "Sent-folder reconciliation unavailable",
            exc_info=True,
            extra={"event": "smtp_sent_folder_reconcile_failed"},
        )
        return False
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
async def imap_fetch_lock(folder_name: str | None = None):
    """Hold a dedicated MySQL connection so commits in the work session cannot release ownership."""
    from app.core.database import engine

    lock_name = _imap_lock_name(folder_name or settings.IMAP_FOLDER)
    async with engine.connect() as connection:
        acquired = await connection.scalar(text("SELECT GET_LOCK(:lock_name, 0)"), {"lock_name": lock_name})
        try:
            yield acquired == 1
        finally:
            if acquired == 1:
                await connection.scalar(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name})


async def run_imap_fetch_locked(
    session: AsyncSession,
    *,
    busy_is_error: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    async with imap_fetch_lock(str(kwargs.get("folder_name") or settings.IMAP_FOLDER)) as acquired:
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
    logger.info(
        "IMAP fetch started",
        extra={
            "event": "imap_fetch_started", "job_run_id": job.id, "mailbox": settings.IMAP_USER,
            "folder": folder_name, "fetch_limit": limit, "unseen_only": unseen_only,
        },
    )
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
    sync_state_id: int | None = None
    try:
        client = await _mail_io(_connect)
        uid_validity = await _select_readonly(client, folder_name)
        job.metadata_json = {**dict(job.metadata_json or {}), "uid_validity": uid_validity}
        sync_state: MailboxSyncState | None = None
        if message_id is None:
            sync_state = await get_or_create_sync_state(
                session,
                mailbox_account=settings.IMAP_USER,
                folder_name=folder_name,
                for_update=True,
            )
            sync_state_id = int(sync_state.id)
            validity_result = apply_uid_validity(sync_state, uid_validity)
            if sync_state.sync_mode in {"initializing", "rebaseline"}:
                if sync_state.initial_sync_start_at is None:
                    raise MailboxSyncConfigurationError("IMAP_INITIAL_SYNC_START_AT_REQUIRED")
                uids = await _mail_io(
                    _uid_search,
                    client,
                    message_id=None,
                    since_date=imap_since_date(sync_state.initial_sync_start_at),
                )
            else:
                uids = await _mail_io(
                    _uid_search,
                    client,
                    message_id=None,
                    start_uid=int(sync_state.last_discovered_uid or 0) + 1,
                )
            job.metadata_json = {
                **dict(job.metadata_json or {}),
                "sync_mode": sync_state.sync_mode,
                "uid_validity_result": validity_result,
            }
        else:
            uids = await _mail_io(_uid_search, client, message_id=message_id)
        retry_uid_set: set[str] = set()
        if message_id is None:
            retry_uids = await _due_retry_uids(session, folder_name=folder_name, uid_validity=uid_validity)
            retry_uid_set = set(retry_uids)
            for retry_uid in retry_uids:
                normalized_uid = str(retry_uid)
                if normalized_uid not in uids:
                    uids.append(normalized_uid)
        uids.sort(key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
        selected_uids: list[str] = []
        if message_id is None:
            configured_limit = (
                settings.IMAP_INITIAL_BATCH_SIZE
                if sync_state is not None and sync_state.sync_mode in {"initializing", "rebaseline"}
                else settings.IMAP_INCREMENTAL_LIMIT
            )
            effective_limit = min(max(1, limit), max(1, configured_limit))
            job.metadata_json = {
                **dict(job.metadata_json or {}),
                "effective_limit": effective_limit,
                "fetch_batch_size": max(1, settings.IMAP_FETCH_BATCH_SIZE),
            }
            selected_uids.extend(
                await _pending_discovered_uids(
                    session,
                    folder_name=folder_name,
                    uid_validity=uid_validity,
                    limit=effective_limit,
                )
            )
            remaining = max(0, effective_limit - len(selected_uids))
            if remaining:
                retry_uids_ordered = [uid for uid in retry_uids if uid not in selected_uids]
                selected_uids.extend(retry_uids_ordered[:remaining])
                remaining = max(0, effective_limit - len(selected_uids))
            if remaining and sync_state is not None:
                new_candidates = [uid for uid in uids if uid not in selected_uids and uid not in retry_uid_set]
                selected_uids.extend(
                    await _discover_new_uids(
                        session,
                        state=sync_state,
                        folder_name=folder_name,
                        uid_validity=uid_validity,
                        candidate_uids=new_candidates,
                        limit=remaining,
                    )
                )
                if len(new_candidates) <= remaining and sync_state.sync_mode in {"initializing", "rebaseline"}:
                    mark_initial_complete(sync_state)
            await _commit(session)
        else:
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
                internal_date = await _mail_io(_uid_fetch_internal_date, client, uid)
                payload = payload_from_eml_bytes(raw, mailbox_account=settings.IMAP_USER, folder_name=folder_name)
                payload.raw_eml_sha256 = hashlib.sha256(raw).hexdigest()
                payload.imap_uid = uid
                payload.fetch_job_run_id = job.id
                blobs = attachment_blobs_from_eml_bytes(raw)
                blobs, _attachment_precheck = filter_decorative_attachments(payload, blobs)
                selected_record = await _find_fetch_record(
                    session,
                    mailbox_account=settings.IMAP_USER,
                    folder_name=folder_name,
                    uid_validity=uid_validity,
                    imap_uid=uid,
                )
                payload_precheck = await precheck_email_payload(
                    session,
                    payload,
                    enforce_target_mailbox=True,
                    current_fetch_record_id=selected_record.id if selected_record is not None else None,
                )
                if payload_precheck.status == "missing_message_id":
                    archived = await archive_raw_email(
                        session,
                        payload=payload,
                        raw_eml=raw,
                        raw_file_name=f"imap-{uid}.eml",
                        user_id=user_id,
                    )
                    fetch_record = await _save_fetch_result(
                        session,
                        mailbox_account=settings.IMAP_USER,
                        folder_name=folder_name,
                        uid_validity=uid_validity,
                        imap_uid=uid,
                        message_id=None,
                        fetch_job_run_id=job.id,
                        email_id=None,
                        duplicate=False,
                        fetch_status="terminal_manual",
                        error_message="MISSING_MESSAGE_ID",
                    )
                    fetch_record.raw_eml_oss_object_id = archived.raw_object_id
                    fetch_record.raw_eml_sha256 = archived.source_content_sha256
                    fetch_record.internal_date = internal_date
                    fetch_record.raw_retention_mode = "permanent"
                    fetch_record.processing_stage = "header_validation_failed"
                    session.add(
                        ManualReviewTask(
                            task_type="missing_message_id",
                            priority="high",
                            status="pending",
                            description="入站邮件缺少 RFC Message-ID，已保留原始 EML，禁止进入自动业务链。",
                            trigger_reason="MISSING_MESSAGE_ID",
                            recovery_stage="header_validation",
                            recovery_action=f"核对邮箱 {settings.IMAP_USER} / {folder_name} / UID {uid} 的原始邮件。",
                        )
                    )
                    fetched.append(
                        {
                            "uid": uid,
                            "message_id": None,
                            "email_id": None,
                            "duplicate": False,
                            "fetch_status": "terminal_manual",
                            "precheck": payload_precheck.to_dict(),
                        }
                    )
                    job.success_count += 1
                    await _commit(session)
                    continue
                if not payload_precheck.accepted:
                    skipped_count += 1
                    await _save_fetch_result(
                        session,
                        mailbox_account=settings.IMAP_USER,
                        folder_name=folder_name,
                        uid_validity=uid_validity,
                        imap_uid=uid,
                        message_id=payload_precheck.message_id or payload.message_id,
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

                archived = await archive_raw_email(
                    session,
                    payload=payload,
                    raw_eml=raw,
                    raw_file_name=f"imap-{uid}.eml",
                    user_id=user_id,
                )
                fetch_record = await _save_fetch_result(
                    session,
                    mailbox_account=settings.IMAP_USER,
                    folder_name=folder_name,
                    uid_validity=uid_validity,
                    imap_uid=uid,
                    message_id=payload.message_id,
                    fetch_job_run_id=job.id,
                    email_id=None,
                    duplicate=False,
                    fetch_status="spooled",
                )
                fetch_record.raw_eml_oss_object_id = archived.raw_object_id
                fetch_record.raw_eml_sha256 = archived.source_content_sha256
                fetch_record.internal_date = internal_date
                fetch_record.raw_retention_mode = "temporary"
                fetch_record.processing_stage = "spooled"
                await session.flush()
                processing_job = await enqueue_job(
                    session,
                    job_type="mail_ingress_process",
                    resource_type="mail_fetch_record",
                    resource_id=fetch_record.id,
                    idempotency_key=f"mail_ingress:{fetch_record.id}",
                    metadata={"user_id": user_id, "auto_parse": auto_parse},
                )
                fetched.append(
                    {
                        "uid": uid,
                        "message_id": payload.message_id,
                        "email_id": None,
                        "duplicate": False,
                        "fetch_status": "spooled",
                        "processing_job_id": processing_job.id,
                    }
                )
                job.success_count += 1
                await _commit(session)
            except Exception as exc:
                logger.exception(
                    "IMAP message processing failed",
                    extra={
                        "event": "imap_message_failed", "job_run_id": job_id, "imap_uid": uid,
                        "folder": folder_name, "error_type": exc.__class__.__name__,
                    },
                )
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
                        message_id=None,
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
        if sync_state_id is not None:
            durable_state = await session.get(MailboxSyncState, sync_state_id, with_for_update=True)
            completed_uids = [
                int(row["uid"])
                for row in fetched
                if str(row.get("uid") or "").isdigit()
                and row.get("fetch_status") in {
                    "spooled", "terminal_manual", "duplicate_message_skipped",
                    "self_sent_mail_skipped", "recipient_not_target_mailbox", "irrelevant_skipped",
                }
            ]
            if durable_state is not None and completed_uids:
                durable_state.last_fetched_uid = max(
                    int(durable_state.last_fetched_uid or 0),
                    max(completed_uids),
                )
                durable_state.last_success_at = utcnow()
                durable_state.last_error_code = None
        job.status = "success" if not failures else "partial_success"
    except Exception as exc:
        job.status = "failed"
        job.error_code = safe_error_code(exc, "IMAP_FETCH_FAILED")
        job.error_message = exc.__class__.__name__
        await _commit(session)
        logger.exception(
            "IMAP fetch failed",
            extra={"event": "imap_fetch_failed", "job_run_id": job.id, "folder": folder_name, "error_code": job.error_code},
        )
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

    (logger.info if not failures else logger.warning)(
        "IMAP fetch completed",
        extra={
            "event": "imap_fetch_completed", "job_run_id": job.id, "folder": folder_name,
            "processed_count": job.processed_count, "success_count": job.success_count,
            "failed_count": job.failed_count, "skipped_count": skipped_count,
            "duration_ms": job.duration_ms, "status": job.status,
        },
    )

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
