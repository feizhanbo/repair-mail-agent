from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.schemas.business import EmailIngestRequest


@dataclass(frozen=True)
class AttachmentPrecheckResult:
    kept_count: int
    skipped_decorative_count: int
    skipped: tuple[dict[str, Any], ...]


def image_dimensions(content: bytes) -> tuple[int, int] | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
    if content[:6] in {b"GIF87a", b"GIF89a"} and len(content) >= 10:
        return int.from_bytes(content[6:8], "little"), int.from_bytes(content[8:10], "little")
    if content.startswith(b"\xff\xd8"):
        index = 2
        start_of_frame = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
        while index + 9 < len(content):
            if content[index] != 0xFF:
                index += 1
                continue
            marker = content[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(content):
                break
            segment_size = int.from_bytes(content[index:index + 2], "big")
            if marker in start_of_frame and index + 7 < len(content):
                height = int.from_bytes(content[index + 3:index + 5], "big")
                width = int.from_bytes(content[index + 5:index + 7], "big")
                return width, height
            index += max(segment_size, 2)
    return None


def filter_decorative_attachments(
    payload: EmailIngestRequest,
    attachment_blobs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], AttachmentPrecheckResult]:
    if len(payload.attachments) != len(attachment_blobs):
        raise ValueError("ATTACHMENT_BLOB_COUNT_MISMATCH")

    kept_payloads: list[dict[str, Any]] = []
    kept_blobs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for attachment, blob in zip(payload.attachments, attachment_blobs, strict=True):
        content = blob.get("content")
        content_type = str(blob.get("content_type") or attachment.get("content_type") or "").lower()
        is_inline = bool(attachment.get("is_inline") or blob.get("is_inline"))
        dimensions = image_dimensions(content) if is_inline and content_type.startswith("image/") and isinstance(content, bytes) else None
        decorative = bool(
            dimensions
            and (
                dimensions[0] < settings.INLINE_IMAGE_MIN_PARSE_WIDTH
                or dimensions[1] < settings.INLINE_IMAGE_MIN_PARSE_HEIGHT
            )
        )
        if decorative:
            skipped.append(
                {
                    "file_name": attachment.get("file_name") or blob.get("file_name"),
                    "content_type": content_type,
                    "width": dimensions[0],
                    "height": dimensions[1],
                    "reason": "INLINE_DECORATIVE_SKIPPED",
                }
            )
            continue
        kept_payloads.append(attachment)
        kept_blobs.append(blob)

    payload.attachments = kept_payloads
    return kept_blobs, AttachmentPrecheckResult(
        kept_count=len(kept_blobs),
        skipped_decorative_count=len(skipped),
        skipped=tuple(skipped),
    )
