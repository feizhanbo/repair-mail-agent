from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from app.attachments.safety import ArchiveSafetyResult, inspect_archive_safety
from app.config import settings
from app.schemas.business import EmailIngestRequest
from app.services.common import utcnow


ARCHIVE_CONTENT_TYPES = {
    "zip": "application/zip",
    "rar": "application/vnd.rar",
    "7z": "application/x-7z-compressed",
    "tar": "application/x-tar",
    "tar_gz": "application/gzip",
    "gzip": "application/gzip",
}
ARCHIVE_MIME_FORMATS = {
    "application/zip": "zip",
    "application/x-zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/rar": "rar",
    "application/vnd.rar": "rar",
    "application/x-rar": "rar",
    "application/x-rar-compressed": "rar",
    "application/x-7z-compressed": "7z",
    "application/x-tar": "tar",
    "application/x-gtar": "tar",
    "application/gzip": "gzip",
    "application/x-gzip": "gzip",
}
OFFICE_EXTENSIONS = {"docx", "xlsx"}
OFFICE_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@dataclass(frozen=True)
class AttachmentPrecheckResult:
    kept_count: int
    skipped_decorative_count: int
    skipped: tuple[dict[str, Any], ...]


def _normalized_mime(value: Any) -> str:
    return str(value or "").lower().split(";", 1)[0].strip()


def _archive_format_from_name(file_name: str | None) -> str | None:
    name = (file_name or "").lower().strip()
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "tar_gz"
    return {
        ".zip": "zip",
        ".rar": "rar",
        ".7z": "7z",
        ".tar": "tar",
        ".gz": "gzip",
    }.get(PurePath(name).suffix.lower())


def _archive_format_from_magic(content: bytes | None, *, file_name: str | None) -> str | None:
    if not content:
        return None
    if content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if content.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return "rar"
    if content.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if len(content) >= 262 and content[257:262] == b"ustar":
        return "tar"
    if content.startswith(b"\x1f\x8b"):
        return "tar_gz" if (file_name or "").lower().endswith((".tar.gz", ".tgz")) else "gzip"
    return None


def detect_archive_format(
    *,
    file_name: str | None,
    content_type: str | None,
    content: bytes | None = None,
) -> tuple[str | None, list[str]]:
    """Identify archive containers without opening or extracting their members."""
    mime = _normalized_mime(content_type)
    suffix = PurePath(file_name or "").suffix.lower().lstrip(".")
    magic_format = _archive_format_from_magic(content, file_name=file_name)
    is_office_declaration = suffix in OFFICE_EXTENSIONS or mime in OFFICE_MIME_TYPES
    if is_office_declaration and magic_format in {None, "zip"}:
        return None, []

    name_format = _archive_format_from_name(file_name)
    mime_format = ARCHIVE_MIME_FORMATS.get(mime)
    detected = magic_format or name_format or mime_format
    if detected == "gzip" and name_format == "tar_gz":
        detected = "tar_gz"

    def compatible(left: str, right: str) -> bool:
        return left == right or {left, right} <= {"gzip", "tar_gz"}

    warnings: list[str] = []
    declared = [value for value in (name_format, mime_format) if value]
    if magic_format and any(not compatible(magic_format, value) for value in declared):
        warnings.append("TYPE_DECLARATION_MISMATCH")
    elif name_format and mime_format and not compatible(name_format, mime_format):
        warnings.append("TYPE_DECLARATION_MISMATCH")
    elif is_office_declaration and magic_format:
        warnings.append("TYPE_DECLARATION_MISMATCH")
    return detected, warnings


def engineering_reference_metadata(
    archive_format: str,
    warnings: list[str] | None = None,
    *,
    safety: ArchiveSafetyResult | None = None,
) -> dict[str, Any]:
    safety_status = safety.status if safety else "unscanned_archive"
    safety_warnings = list(safety.warnings) if safety else []
    return {
        "file_type": "archive",
        "detected_format": archive_format,
        "attachment_role": "engineering_reference",
        "business_required": False,
        "ai_parse_required": False,
        "blocks_ticket_flow": bool(safety and not safety.safe),
        "security_status": safety_status,
        "security_warnings": safety_warnings,
        "member_count": safety.member_count if safety else None,
        "expanded_size": safety.expanded_size if safety else None,
        "parse_skip_reason": "ENGINEERING_REFERENCE_NOT_REQUIRED",
        "detection_warnings": list(warnings or []),
        "classified_at": utcnow().isoformat(),
    }


def classify_engineering_reference(attachment: dict[str, Any], blob: dict[str, Any]) -> str | None:
    content = blob.get("content")
    archive_format, warnings = detect_archive_format(
        file_name=str(blob.get("file_name") or attachment.get("file_name") or ""),
        content_type=str(blob.get("content_type") or attachment.get("content_type") or ""),
        content=content if isinstance(content, bytes) else None,
    )
    if not archive_format:
        return None

    content_type = ARCHIVE_CONTENT_TYPES[archive_format]
    attachment["content_type"] = content_type
    safety = inspect_archive_safety(content if isinstance(content, bytes) else None, archive_format)
    attachment["parse_status"] = "pending" if safety.safe else "needs_manual_review"
    attachment["extracted_text"] = None
    attachment["extracted_json"] = engineering_reference_metadata(archive_format, warnings, safety=safety)
    attachment["parse_error"] = None if safety.safe else safety.warnings[0]
    blob["content_type"] = content_type
    return archive_format


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
        classify_engineering_reference(attachment, blob)
        kept_payloads.append(attachment)
        kept_blobs.append(blob)

    payload.attachments = kept_payloads
    return kept_blobs, AttachmentPrecheckResult(
        kept_count=len(kept_blobs),
        skipped_decorative_count=len(skipped),
        skipped=tuple(skipped),
    )
