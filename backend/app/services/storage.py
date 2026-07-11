from __future__ import annotations

import hashlib
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import OssObject


class StorageConfigurationError(RuntimeError):
    pass


class StorageUploadError(RuntimeError):
    pass


def _oss_configured() -> bool:
    return bool(settings.OSS_ENDPOINT and settings.OSS_BUCKET and settings.OSS_ACCESS_KEY and settings.OSS_SECRET_KEY)


def _safe_file_name(name: str | None) -> str:
    value = (name or "file").strip().replace("\\", "_").replace("/", "_")
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return (value or "file")[:180]


def _object_key(*, source_type: str, original_file_name: str | None, sha256_hash: str) -> str:
    today = datetime.utcnow()
    safe_name = _safe_file_name(original_file_name)
    return f"{source_type}/{today:%Y/%m/%d}/{sha256_hash[:2]}/{sha256_hash}-{safe_name}"


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

    sha256_hash = hashlib.sha256(content).hexdigest()
    object_key = _object_key(source_type=source_type, original_file_name=original_file_name, sha256_hash=sha256_hash)
    existing = await session.scalar(select(OssObject).where(OssObject.bucket == settings.OSS_BUCKET, OssObject.object_key == object_key))
    if existing is not None and existing.upload_status == "success":
        return existing

    import oss2

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
        oss_object.upload_status = "pending"
        oss_object.error_message = None
        oss_object.file_size = len(content)
        oss_object.content_type = content_type
        oss_object.created_by_user_id = user_id

    try:
        auth = oss2.Auth(settings.OSS_ACCESS_KEY, settings.OSS_SECRET_KEY)
        bucket = oss2.Bucket(auth, settings.OSS_ENDPOINT, settings.OSS_BUCKET)
        headers = {"Content-Type": content_type} if content_type else None
        result = bucket.put_object(object_key, content, headers=headers)
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

    import oss2

    _bucket = bucket or settings.OSS_BUCKET
    _endpoint = endpoint or settings.OSS_ENDPOINT

    auth = oss2.Auth(settings.OSS_ACCESS_KEY, settings.OSS_SECRET_KEY)
    oss_bucket = oss2.Bucket(auth, _endpoint, _bucket)
    return oss_bucket.sign_url("GET", object_key, expires_seconds)


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
