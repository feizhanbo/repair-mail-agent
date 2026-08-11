from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Email, EmailAttachment, JobRunLog, OssObject, ReplyRecord, TicketRma
from app.services.common import utcnow


class StorageConfigurationError(RuntimeError):
    pass


class StorageUploadError(RuntimeError):
    pass


class StorageDeleteError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class OssDeleteResult:
    bucket: str
    object_key: str
    deleted: bool
    already_missing: bool = False
    error_code: str | None = None


_oss_semaphore = asyncio.Semaphore(max(1, settings.OSS_IO_CONCURRENCY))


def _oss_configured() -> bool:
    return bool(settings.OSS_ENDPOINT and settings.OSS_BUCKET and settings.OSS_ACCESS_KEY and settings.OSS_SECRET_KEY)


def _safe_file_name(name: str | None) -> str:
    value = (name or "file").strip().replace("\\", "_").replace("/", "_")
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return (value or "file")[:180]


def _object_key(*, source_type: str, original_file_name: str | None, sha256_hash: str) -> str:
    safe_source = _safe_file_name(source_type)
    safe_name = _safe_file_name(original_file_name)
    return f"{safe_source}/{sha256_hash[:2]}/{sha256_hash}-{safe_name}"


def normalized_content_type(file_name: str | None, declared: str | None) -> str | None:
    value = (declared or "").split(";", 1)[0].strip().lower()
    if value and value not in {"application/octet-stream", "binary/octet-stream"}:
        return value
    guessed, _ = mimetypes.guess_type(file_name or "")
    return guessed or value or None


def _build_bucket(*, endpoint: str, bucket_name: str):
    import oss2

    auth = oss2.Auth(settings.OSS_ACCESS_KEY, settings.OSS_SECRET_KEY)
    return oss2.Bucket(auth, endpoint, bucket_name)


async def upload_bytes_to_oss(
    session: AsyncSession,
    *,
    content: bytes,
    original_file_name: str | None,
    content_type: str | None,
    source_type: str,
    user_id: int | None = None,
) -> OssObject:
    if not _oss_configured():
        raise StorageConfigurationError("OSS_NOT_CONFIGURED")

    content_type = normalized_content_type(original_file_name, content_type)
    sha256_hash = hashlib.sha256(content).hexdigest()
    object_key = _object_key(source_type=source_type, original_file_name=original_file_name, sha256_hash=sha256_hash)
    existing = await session.scalar(select(OssObject).where(OssObject.bucket == settings.OSS_BUCKET, OssObject.object_key == object_key))
    metadata_requires_reupload = False
    if existing is not None and existing.upload_status == "success":
        if not content_type or existing.content_type == content_type:
            return existing
        metadata_requires_reupload = True
        existing.upload_status = "pending"
        existing.content_type = content_type

    safe_name = _safe_file_name(original_file_name)
    if existing is None:
        oss_object = OssObject(
            bucket=settings.OSS_BUCKET,
            endpoint=settings.OSS_ENDPOINT,
            object_key=object_key,
            original_file_name=original_file_name,
            safe_file_name=safe_name,
            content_type=content_type,
            file_size=len(content),
            sha256_hash=sha256_hash,
            source_type=source_type,
            upload_status="pending",
            created_by_user_id=user_id,
        )
        session.add(oss_object)
        await session.flush()
    else:
        oss_object = existing
        if oss_object.upload_status == "pending" and not metadata_requires_reupload:
            try:
                async with _oss_semaphore:
                    exists = await asyncio.to_thread(
                        _build_bucket(endpoint=settings.OSS_ENDPOINT, bucket_name=settings.OSS_BUCKET).object_exists,
                        object_key,
                    )
                if exists:
                    oss_object.upload_status = "success"
                    oss_object.error_message = None
                    return oss_object
            except Exception:
                pass
        oss_object.upload_status = "pending"
        oss_object.error_message = None
        oss_object.file_size = len(content)
        oss_object.content_type = content_type
        oss_object.created_by_user_id = user_id

    try:
        headers = {"Content-Type": content_type} if content_type else None
        async with _oss_semaphore:
            result = await asyncio.to_thread(
                _build_bucket(endpoint=settings.OSS_ENDPOINT, bucket_name=settings.OSS_BUCKET).put_object,
                object_key,
                content,
                headers=headers,
            )
    except Exception as exc:
        oss_object.upload_status = "failed"
        oss_object.error_message = "OSS upload failed"
        raise StorageUploadError("OSS_UPLOAD_FAILED") from exc

    oss_object.upload_status = "success"
    oss_object.etag = getattr(result, "etag", None)
    oss_object.error_message = None
    return oss_object


async def generate_presigned_url(
    session: AsyncSession,
    *,
    object_key: str,
    expires_seconds: int = 3600,
    bucket: str | None = None,
    endpoint: str | None = None,
) -> str:
    if not _oss_configured():
        raise StorageConfigurationError("OSS_NOT_CONFIGURED")

    bucket_name = bucket or settings.OSS_BUCKET
    endpoint_name = endpoint or settings.OSS_ENDPOINT
    async with _oss_semaphore:
        return await asyncio.to_thread(
            _build_bucket(endpoint=endpoint_name, bucket_name=bucket_name).sign_url,
            "GET",
            object_key,
            expires_seconds,
        )


async def generate_presigned_url_for_object(
    session: AsyncSession,
    *,
    oss_object_id: int,
    expires_seconds: int = 3600,
) -> str:
    stmt = select(OssObject).where(OssObject.id == oss_object_id)
    oss_object = await session.scalar(stmt)
    if oss_object is None:
        raise ValueError(f"OssObject with id {oss_object_id} not found")

    return await generate_presigned_url(
        session,
        object_key=oss_object.object_key,
        expires_seconds=expires_seconds,
        bucket=oss_object.bucket,
        endpoint=oss_object.endpoint or settings.OSS_ENDPOINT,
    )


async def download_oss_object_bytes(
    session: AsyncSession,
    *,
    oss_object_id: int,
) -> bytes:
    if not _oss_configured():
        raise StorageConfigurationError("OSS_NOT_CONFIGURED")

    oss_object = await session.scalar(select(OssObject).where(OssObject.id == oss_object_id))
    if oss_object is None:
        raise ValueError(f"OssObject with id {oss_object_id} not found")
    if oss_object.upload_status != "success":
        raise StorageUploadError("OSS_OBJECT_NOT_READY")
    if oss_object.upload_status != "success":
        raise StorageUploadError("OSS_OBJECT_NOT_READY")

    try:
        def _download() -> bytes:
            bucket_client = _build_bucket(
                endpoint=oss_object.endpoint or settings.OSS_ENDPOINT,
                bucket_name=oss_object.bucket,
            )
            return bucket_client.get_object(oss_object.object_key).read()

        async with _oss_semaphore:
            return await asyncio.to_thread(_download)
    except Exception as exc:
        raise StorageUploadError("OSS_DOWNLOAD_FAILED") from exc


async def oss_object_exists(
    *, bucket: str, object_key: str, endpoint: str | None = None
) -> bool:
    if not _oss_configured():
        raise StorageConfigurationError("OSS_NOT_CONFIGURED")
    try:
        async with _oss_semaphore:
            return bool(
                await asyncio.to_thread(
                    _build_bucket(
                        endpoint=endpoint or settings.OSS_ENDPOINT,
                        bucket_name=bucket,
                    ).object_exists,
                    object_key,
                )
            )
    except Exception as exc:
        raise StorageDeleteError("OSS_EXISTS_CHECK_FAILED") from exc


async def delete_oss_object(
    *,
    bucket: str,
    object_key: str,
    endpoint: str | None = None,
    object_version: str | None = None,
) -> OssDeleteResult:
    """Idempotently delete an OSS object using persisted identity fields."""
    if not _oss_configured():
        raise StorageConfigurationError("OSS_NOT_CONFIGURED")
    try:
        client = _build_bucket(
            endpoint=endpoint or settings.OSS_ENDPOINT,
            bucket_name=bucket,
        )
        async with _oss_semaphore:
            existed = bool(await asyncio.to_thread(client.object_exists, object_key))
            if not existed:
                return OssDeleteResult(bucket, object_key, True, already_missing=True)
            if object_version:
                await asyncio.to_thread(
                    client.delete_object,
                    object_key,
                    params={"versionId": object_version},
                )
                remains = False
            else:
                await asyncio.to_thread(client.delete_object, object_key)
                remains = bool(await asyncio.to_thread(client.object_exists, object_key))
        if remains:
            raise StorageDeleteError("OSS_DELETE_NOT_CONFIRMED")
        return OssDeleteResult(bucket, object_key, True)
    except StorageDeleteError:
        raise
    except Exception as exc:
        status_code = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        provider_code = str(getattr(exc, "code", "") or getattr(exc, "error_code", ""))
        code = (
            "OSS_DELETE_FORBIDDEN"
            if status_code == 403
            or exc.__class__.__name__ in {"AccessDenied", "NoPermission"}
            or provider_code in {"AccessDenied", "Forbidden", "NoPermission"}
            else "OSS_DELETE_FAILED"
        )
        raise StorageDeleteError(code, retryable=code != "OSS_DELETE_FORBIDDEN") from exc


async def delete_oss_objects(objects: list[dict]) -> list[OssDeleteResult]:
    results: list[OssDeleteResult] = []
    for item in objects:
        try:
            results.append(
                await delete_oss_object(
                    bucket=str(item["bucket"]),
                    object_key=str(item["object_key"]),
                    endpoint=item.get("endpoint"),
                    object_version=item.get("object_version"),
                )
            )
        except (StorageConfigurationError, StorageDeleteError) as exc:
            results.append(
                OssDeleteResult(
                    str(item.get("bucket") or ""),
                    str(item.get("object_key") or ""),
                    False,
                    error_code=str(exc),
                )
            )
    return results


async def oss_reference_summary(session: AsyncSession, oss_object_id: int) -> dict[str, int]:
    checks = {
        "emails.raw_eml_oss_object_id": select(Email.id).where(Email.raw_eml_oss_object_id == oss_object_id),
        "email_attachments.oss_object_id": select(EmailAttachment.id).where(EmailAttachment.oss_object_id == oss_object_id),
        "reply_records.rma_pdf_oss_object_id": select(ReplyRecord.id).where(ReplyRecord.rma_pdf_oss_object_id == oss_object_id),
        "ticket_rmas.pdf_oss_object_id": select(TicketRma.id).where(TicketRma.pdf_oss_object_id == oss_object_id),
        "job_run_logs.input_oss_object_id": select(JobRunLog.id).where(JobRunLog.input_oss_object_id == oss_object_id),
        "job_run_logs.output_oss_object_id": select(JobRunLog.id).where(JobRunLog.output_oss_object_id == oss_object_id),
    }
    summary: dict[str, int] = {}
    for name, statement in checks.items():
        summary[name] = len((await session.execute(statement)).scalars().all())
    return summary


async def find_orphan_oss_objects(
    session: AsyncSession,
    *,
    older_than_hours: int | None = None,
    limit: int = 100,
) -> list[OssObject]:
    cutoff = utcnow() - timedelta(hours=older_than_hours or settings.OSS_ORPHAN_MIN_AGE_HOURS)
    statement = (
        select(OssObject)
        .where(
            OssObject.created_at < cutoff,
            ~exists(select(Email.id).where(Email.raw_eml_oss_object_id == OssObject.id)),
            ~exists(select(EmailAttachment.id).where(EmailAttachment.oss_object_id == OssObject.id)),
            ~exists(select(JobRunLog.id).where(JobRunLog.input_oss_object_id == OssObject.id)),
            ~exists(select(JobRunLog.id).where(JobRunLog.output_oss_object_id == OssObject.id)),
            ~exists(select(ReplyRecord.id).where(ReplyRecord.rma_pdf_oss_object_id == OssObject.id)),
            ~exists(select(TicketRma.id).where(TicketRma.pdf_oss_object_id == OssObject.id)),
        )
        .order_by(OssObject.created_at.asc())
        .limit(max(1, min(limit, 1000)))
    )
    return list((await session.execute(statement)).scalars().all())
