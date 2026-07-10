from __future__ import annotations

import imaplib
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import JobRunLog
from app.services import emails as email_service
from app.services.common import utcnow
from app.services.eml import attachment_blobs_from_eml_bytes, payload_from_eml_bytes
from app.services.storage import upload_bytes_to_oss


class ImapConfigurationError(RuntimeError):
    pass


class ImapFetchError(RuntimeError):
    pass


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
    started = time.monotonic()
    job = JobRunLog(
        job_name="imap_fetch_now",
        job_type="imap_fetch",
        status="running",
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
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = _connect()
        typ, _ = client.select(folder_name, readonly=True)
        if typ != "OK":
            raise ImapFetchError("IMAP_SELECT_FAILED")
        uids = _uid_search(client, message_id=message_id, unseen_only=unseen_only)
        if limit > 0:
            uids = uids[-limit:]
        job.processed_count = len(uids)

        for uid in uids:
            try:
                raw = _uid_fetch_raw(client, uid)
                payload = payload_from_eml_bytes(raw, mailbox_account=settings.IMAP_USER, folder_name=folder_name)
                payload.imap_uid = uid
                payload.fetch_job_run_id = job.id
                if archive_to_oss:
                    raw_object = await upload_bytes_to_oss(
                        session,
                        content=raw,
                        original_file_name=f"imap-{uid}.eml",
                        content_type="message/rfc822",
                        source_type="raw_eml",
                        user_id=user_id,
                    )
                    payload.raw_eml_oss_object_id = raw_object.id
                    blobs = attachment_blobs_from_eml_bytes(raw)
                    for attachment, blob in zip(payload.attachments, blobs, strict=False):
                        attachment_object = await upload_bytes_to_oss(
                            session,
                            content=blob["content"],
                            original_file_name=blob["file_name"],
                            content_type=blob.get("content_type"),
                            source_type="email_attachment",
                            user_id=user_id,
                        )
                        attachment["oss_object_id"] = attachment_object.id
                ingest_result = await email_service.ingest_email(session, payload=payload, user_id=user_id, auto_parse=auto_parse)
                fetched.append(
                    {
                        "uid": uid,
                        "message_id": payload.message_id,
                        "email_id": ingest_result.get("email", {}).get("id"),
                        "duplicate": ingest_result.get("duplicate", False),
                        "parse_status": ingest_result.get("email", {}).get("parse_status"),
                    }
                )
                job.success_count += 1
            except Exception as exc:
                failures.append({"uid": uid, "error": exc.__class__.__name__})
                job.failed_count += 1
        job.status = "success" if not failures else "partial_success"
    except Exception as exc:
        job.status = "failed"
        job.error_message = exc.__class__.__name__
        raise
    finally:
        job.finished_at = utcnow()
        job.duration_ms = int((time.monotonic() - started) * 1000)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass

    return {
        "job_id": job.id,
        "status": job.status,
        "processed_count": job.processed_count,
        "success_count": job.success_count,
        "failed_count": job.failed_count,
        "fetched": fetched,
        "failures": failures,
    }
