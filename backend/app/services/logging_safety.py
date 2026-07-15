from __future__ import annotations

import re
from typing import Any

from app.services.common import sha256_text


_SECRET_KEY_PARTS = (
    "api_key",
    "access_key",
    "secret",
    "password",
    "authorization",
    "token",
    "signature",
)
_CONTENT_KEY_PARTS = (
    "body",
    "content",
    "raw_text",
    "extracted_text",
    "request_payload",
    "response_payload",
    "raw_output",
    "messages",
    "signed_url",
    "presigned_url",
    "reason",
    "description",
)
_SIGNED_QUERY = re.compile(
    r"([?&](?:OSSAccessKeyId|Signature|Expires|x-oss-[^=]+)=[^&\s]+)",
    flags=re.IGNORECASE,
)


def text_fingerprint(value: str) -> dict[str, Any]:
    return {
        "redacted": True,
        "chars": len(value),
        "sha256": sha256_text(value),
    }


def safe_error_code(error: Exception | str | None, default: str = "INTERNAL_ERROR") -> str | None:
    if error is None:
        return None
    detail = getattr(error, "detail", None)
    candidate = detail if isinstance(detail, str) else str(error)
    text = candidate.strip().upper().replace(" ", "_")
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,99}", text):
        return text
    known = (
        "TIMEOUT",
        "HTTP_429",
        "OUTPUT_NOT_JSON",
        "OUTPUT_SCHEMA_INVALID",
        "OSS_NOT_CONFIGURED",
        "OSS_ARCHIVAL_FAILED",
        "FILE_TOO_LARGE",
        "SMTP_RECIPIENT_NOT_ALLOWED",
    )
    for code in known:
        if code in text:
            return code
    http_match = re.search(r"HTTP_([45][0-9]{2})", text)
    if http_match:
        return http_match.group(0)
    return default


def sanitize_log_payload(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"

    if isinstance(value, str):
        if any(part in lowered for part in _CONTENT_KEY_PARTS):
            return text_fingerprint(value)
        return _SIGNED_QUERY.sub("[REDACTED_QUERY]", value)[:2000]
    if isinstance(value, dict):
        return {
            str(item_key)[:100]: sanitize_log_payload(item_value, key=str(item_key))
            for item_key, item_value in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_log_payload(item, key=key) for item in list(value)[:100]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]
