from __future__ import annotations

import hashlib
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from app.schemas.business import EmailIngestRequest


def _parse_message(content: bytes):
    return BytesParser(policy=policy.default).parsebytes(content)


def _header(message, name: str) -> str | None:
    value = message.get(name)
    return str(value) if value is not None else None


def _message_text_parts(content: bytes) -> tuple[str | None, str | None]:
    message = _parse_message(content)
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            try:
                part_content = part.get_content()
            except Exception:
                continue
            if not isinstance(part_content, str):
                continue
            if content_type == "text/plain":
                plain_parts.append(part_content)
            elif content_type == "text/html":
                html_parts.append(part_content)
    else:
        try:
            part_content = message.get_content()
        except Exception:
            part_content = None
        if isinstance(part_content, str):
            if message.get_content_type() == "text/html":
                html_parts.append(part_content)
            else:
                plain_parts.append(part_content)
    return ("\n".join(plain_parts).strip() or None, "\n".join(html_parts).strip() or None)


def _iter_attachment_parts(content: bytes):
    message = _parse_message(content)
    for part in message.walk():
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition not in {"attachment", "inline"} and not filename:
            continue
        yield part, disposition, filename


def _attachment_items(content: bytes) -> list[dict]:
    attachments: list[dict] = []
    for part, disposition, filename in _iter_attachment_parts(content):
        payload = part.get_payload(decode=True) or b""
        extracted_text = None
        if part.get_content_type().startswith("text/"):
            try:
                part_content = part.get_content()
                extracted_text = part_content.strip() if isinstance(part_content, str) else None
            except Exception:
                extracted_text = None
        attachments.append(
            {
                "file_name": filename or "attachment",
                "content_type": part.get_content_type(),
                "file_size": len(payload),
                "file_hash": hashlib.sha256(payload).hexdigest(),
                "is_inline": disposition == "inline",
                "content_id": _header(part, "Content-ID"),
                "parse_status": "parsed" if extracted_text else "pending",
                "extracted_text": extracted_text,
            }
        )
    return attachments


def attachment_blobs_from_eml_bytes(content: bytes) -> list[dict]:
    blobs: list[dict] = []
    for part, disposition, filename in _iter_attachment_parts(content):
        payload = part.get_payload(decode=True) or b""
        blobs.append(
            {
                "file_name": filename or "attachment",
                "content_type": part.get_content_type(),
                "file_size": len(payload),
                "file_hash": hashlib.sha256(payload).hexdigest(),
                "is_inline": disposition == "inline",
                "content_id": _header(part, "Content-ID"),
                "content": payload,
            }
        )
    return blobs


def payload_from_eml_bytes(content: bytes, *, mailbox_account: str = "manual-eml", folder_name: str | None = "INBOX") -> EmailIngestRequest:
    message = _parse_message(content)
    from_header = _header(message, "From")
    from_address = parseaddr(from_header or "")[1] or from_header
    if not from_address:
        raise ValueError("EML_FROM_ADDRESS_REQUIRED")
    text_body, html_body = _message_text_parts(content)
    sent_at = None
    date_header = _header(message, "Date")
    if date_header:
        try:
            sent_at = parsedate_to_datetime(date_header)
        except Exception:
            sent_at = None
    return EmailIngestRequest(
        mailbox_account=mailbox_account,
        folder_name=folder_name,
        message_id=_header(message, "Message-ID"),
        in_reply_to=_header(message, "In-Reply-To"),
        references_header=_header(message, "References"),
        from_address=from_address,
        to_addresses=_header(message, "To"),
        cc_addresses=_header(message, "Cc"),
        subject=_header(message, "Subject"),
        text_body=text_body,
        html_body=html_body,
        sent_at=sent_at,
        attachments=_attachment_items(content),
    )
