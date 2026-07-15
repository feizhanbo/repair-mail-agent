from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.business import EmailIngestRequest
from app.services.audit import log_system_event
from app.services.storage import (
    StorageConfigurationError,
    StorageUploadError,
    upload_bytes_to_oss,
    normalized_content_type,
)


class EmailArchivalError(RuntimeError):
    def __init__(self, code: str, *, stage: str, object_ids: list[int] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.object_ids = object_ids or []


@dataclass
class ArchiveBundleResult:
    raw_object_id: int
    attachment_object_ids: list[int] = field(default_factory=list)
    source_content_sha256: str = ""


def validate_archive_bundle(raw_eml: bytes, attachment_blobs: list[dict[str, Any]]) -> None:
    if not raw_eml:
        raise EmailArchivalError("EML_FILE_EMPTY", stage="validate")
    if len(attachment_blobs) > settings.EMAIL_MAX_ATTACHMENTS:
        raise EmailArchivalError("TOO_MANY_ATTACHMENTS", stage="validate")
    if len(raw_eml) > settings.EMAIL_MAX_ARCHIVE_BYTES:
        raise EmailArchivalError("EMAIL_ARCHIVE_TOO_LARGE", stage="validate")
    total_size = len(raw_eml)
    for blob in attachment_blobs:
        content = blob.get("content")
        if not isinstance(content, bytes):
            raise EmailArchivalError("ATTACHMENT_CONTENT_REQUIRED", stage="validate")
        if len(content) > settings.ATTACHMENT_MAX_ARCHIVE_BYTES:
            raise EmailArchivalError("ATTACHMENT_ARCHIVE_TOO_LARGE", stage="validate")
        total_size += len(content)
    if total_size > settings.EMAIL_MAX_ARCHIVE_BYTES:
        raise EmailArchivalError("EMAIL_ARCHIVE_TOO_LARGE", stage="validate")


async def archive_email_bundle(
    session: AsyncSession,
    *,
    payload: EmailIngestRequest,
    raw_eml: bytes,
    raw_file_name: str,
    attachment_blobs: list[dict[str, Any]],
    source: str,
    user_id: int | None = None,
    correlation_id: str | None = None,
) -> ArchiveBundleResult:
    validate_archive_bundle(raw_eml, attachment_blobs)
    if len(payload.attachments) != len(attachment_blobs):
        raise EmailArchivalError("ATTACHMENT_BLOB_COUNT_MISMATCH", stage="validate")

    started = time.perf_counter()
    object_ids: list[int] = []
    source_hash = hashlib.sha256(raw_eml).hexdigest()
    try:
        raw_object = await upload_bytes_to_oss(
            session,
            content=raw_eml,
            original_file_name=raw_file_name,
            content_type="message/rfc822",
            source_type="raw_eml",
            user_id=user_id,
        )
        object_ids.append(raw_object.id)
        payload.raw_eml_oss_object_id = raw_object.id
        payload.raw_eml_sha256 = source_hash

        for index, (attachment, blob) in enumerate(
            zip(payload.attachments, attachment_blobs, strict=True)
        ):
            content = blob["content"]
            file_name = blob.get("file_name") or attachment.get("file_name")
            content_type = normalized_content_type(
                file_name,
                blob.get("content_type") or attachment.get("content_type"),
            )
            attachment_object = await upload_bytes_to_oss(
                session,
                content=content,
                original_file_name=file_name,
                content_type=content_type,
                source_type="email_attachment",
                user_id=user_id,
            )
            object_ids.append(attachment_object.id)
            attachment["oss_object_id"] = attachment_object.id
            attachment["content_type"] = content_type
            attachment["file_size"] = len(content)
            attachment["file_hash"] = hashlib.sha256(content).hexdigest()

        await log_system_event(
            session,
            event_type="email_archival",
            module_name="email_archival",
            event_stage="oss_archive",
            event_status="success",
            target_type="email_source",
            correlation_id=correlation_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            message="Email source and attachments archived",
            details={
                "source": source,
                "source_sha256": source_hash,
                "object_ids": object_ids,
                "attachment_count": len(attachment_blobs),
            },
        )
        return ArchiveBundleResult(
            raw_object_id=raw_object.id,
            attachment_object_ids=object_ids[1:],
            source_content_sha256=source_hash,
        )
    except StorageConfigurationError as exc:
        code = "OSS_NOT_CONFIGURED"
        await log_system_event(
            session,
            event_type="email_archival",
            module_name="email_archival",
            event_stage="oss_archive",
            event_status="failed",
            target_type="email_source",
            correlation_id=correlation_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_code=code,
            severity="error",
            message="Email archival configuration unavailable",
            details={"source": source, "source_sha256": source_hash, "object_ids": object_ids},
        )
        raise EmailArchivalError(code, stage="configuration", object_ids=object_ids) from exc
    except StorageUploadError as exc:
        code = "OSS_ARCHIVAL_FAILED"
        await log_system_event(
            session,
            event_type="email_archival",
            module_name="email_archival",
            event_stage="oss_archive",
            event_status="failed",
            target_type="email_source",
            correlation_id=correlation_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_code=code,
            severity="error",
            message="Email archival failed",
            details={"source": source, "source_sha256": source_hash, "object_ids": object_ids},
        )
        raise EmailArchivalError(code, stage="upload", object_ids=object_ids) from exc
