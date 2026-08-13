from __future__ import annotations

from pathlib import PurePath


SUPPORTED_ATTACHMENT_TYPES = {"docx", "xlsx", "csv", "txt", "prc", "html", "image", "pdf"}


def detect_file_type(*, file_name: str | None, content_type: str | None) -> str | None:
    """Determine parser type from deterministic MIME and extension rules."""
    mime = (content_type or "").lower().split(";", 1)[0].strip()
    suffix = PurePath(file_name or "").suffix.lower().lstrip(".")
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf" or suffix == "pdf":
        return "pdf"
    if suffix in {"docx", "xlsx", "csv", "txt", "prc", "html", "htm"}:
        return "html" if suffix == "htm" else suffix
    if mime in {"text/plain", "application/json", "application/xml", "text/xml"}:
        return "txt"
    if mime in {"text/csv", "application/csv"}:
        return "csv"
    if mime in {"text/html", "application/xhtml+xml"}:
        return "html"
    return None
