from __future__ import annotations

import hashlib
from datetime import timezone
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime, parseaddr
from typing import Any

from app.schemas.business import EmailIngestRequest


def _header_text(message: Message, name: str) -> str | None:
    value = message.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _addresses(message: Message, name: str) -> str | None:
    headers = message.get_all(name, [])
    parsed = [address for _, address in getaddresses(headers) if address]
    if parsed:
        return ", ".join(parsed)
    value = _header_text(message, name)
    return value


def _message_datetime(message: Message):
    value = _header_text(message, "Date")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _part_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    if isinstance(part, EmailMessage):
        content = part.get_content()
        if isinstance(content, str):
            return content.encode(part.get_content_charset() or "utf-8", errors="replace")
    value = part.get_payload()
    if isinstance(value, str):
        return value.encode(part.get_content_charset() or "utf-8", errors="replace")
    return b""


def _part_text(part: Message) -> str:
    if isinstance(part, EmailMessage):
        try:
            content = part.get_content()
            if isinstance(content, str):
                return content
        except Exception:
            pass
    data = _part_bytes(part)
    charset = part.get_content_charset() or "utf-8"
    return data.decode(charset, errors="replace")


def _content_id(part: Message) -> str | None:
    value = part.get("Content-ID")
    if not value:
        return None
    return str(value).strip().strip("<>").strip() or None


def _is_attachment(part: Message) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True
    if part.get_filename():
        return True
    if disposition == "inline" and _content_id(part) and not part.get_content_type().startswith("text/"):
        return True
    return False


def _attachment_name(part: Message, index: int) -> str:
    filename = part.get_filename()
    if filename:
        return str(filename)
    extension = part.get_content_subtype() or "bin"
    prefix = "inline" if (part.get_content_disposition() or "").lower() == "inline" else "attachment"
    return f"{prefix}-{index}.{extension}"


def _parse_eml(raw: bytes) -> tuple[Message, list[dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise ValueError("EML_PARSE_FAILED") from exc

    attachments: list[dict[str, Any]] = []
    blobs: list[dict[str, Any]] = []
    plain_parts: list[str] = []
    html_parts: list[str] = []

    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        if _is_attachment(part):
            content = _part_bytes(part)
            index = len(attachments) + 1
            file_name = _attachment_name(part, index)
            content_id = _content_id(part)
            is_inline = (
                (part.get_content_disposition() or "").lower() == "inline"
                or (bool(content_id) and (part.get_content_disposition() or "").lower() != "attachment")
            )
            metadata = {
                "file_name": file_name,
                "content_type": content_type,
                "file_size": len(content),
                "file_hash": hashlib.sha256(content).hexdigest(),
                "is_inline": is_inline,
                "content_id": content_id,
                "parse_status": "pending",
            }
            attachments.append(metadata)
            blobs.append(
                {
                    "file_name": file_name,
                    "content_type": content_type,
                    "content": content,
                    "file_hash": metadata["file_hash"],
                    "is_inline": metadata["is_inline"],
                    "content_id": content_id,
                }
            )
            continue
        if content_type == "text/plain":
            plain_parts.append(_part_text(part))
        elif content_type == "text/html":
            html_parts.append(_part_text(part))

    plain = "\n".join(part.strip() for part in plain_parts if part and part.strip()) or None
    html = "\n".join(part.strip() for part in html_parts if part and part.strip()) or None
    return message, attachments, blobs, plain, html


def payload_from_eml_bytes(
    raw: bytes,
    *,
    mailbox_account: str,
    folder_name: str | None = "INBOX",
) -> EmailIngestRequest:
    if not raw:
        raise ValueError("EML_FILE_EMPTY")
    message, attachments, _blobs, plain, html = _parse_eml(raw)
    from_address = parseaddr(_header_text(message, "From") or "")[1]
    if not from_address:
        raise ValueError("EML_FROM_REQUIRED")
    return EmailIngestRequest(
        mailbox_account=mailbox_account,
        folder_name=folder_name,
        message_id=_header_text(message, "Message-ID"),
        in_reply_to=_header_text(message, "In-Reply-To"),
        references_header=_header_text(message, "References"),
        from_address=from_address,
        to_addresses=_addresses(message, "To"),
        cc_addresses=_addresses(message, "Cc"),
        subject=_header_text(message, "Subject"),
        text_body=plain,
        html_body=html,
        sent_at=_message_datetime(message),
        attachments=attachments,
    )


def attachment_blobs_from_eml_bytes(raw: bytes) -> list[dict[str, Any]]:
    if not raw:
        return []
    _message, _attachments, blobs, _plain, _html = _parse_eml(raw)
    return blobs
