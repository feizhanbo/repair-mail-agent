from __future__ import annotations

import hashlib
from email.message import EmailMessage

import pytest

from app.services.eml import attachment_blobs_from_eml_bytes, payload_from_eml_bytes


def _sample_eml() -> bytes:
    message = EmailMessage()
    message["From"] = "Customer <customer@example.com>"
    message["To"] = "Repair <repair@example.com>"
    message["Cc"] = "cc@example.com"
    message["Subject"] = "Repair SN001"
    message["Message-ID"] = "<sample-1@example.com>"
    message["Date"] = "Mon, 13 Jul 2026 10:00:00 +0800"
    message.set_content("Please repair SN001.\n")
    message.add_alternative("<p>Please repair <b>SN001</b>.</p>", subtype="html")
    message.add_attachment(b"fault log", maintype="text", subtype="plain", filename="fault.txt")
    message.add_attachment(
        b"image-bytes",
        maintype="image",
        subtype="png",
        filename="fault.png",
        cid="<inline-image-1>",
        disposition="inline",
    )
    return message.as_bytes()


def test_payload_from_eml_bytes_extracts_headers_body_and_attachment_metadata() -> None:
    payload = payload_from_eml_bytes(_sample_eml(), mailbox_account="inbox@example.com", folder_name="INBOX")

    assert payload.mailbox_account == "inbox@example.com"
    assert payload.folder_name == "INBOX"
    assert payload.message_id == "<sample-1@example.com>"
    assert payload.from_address == "customer@example.com"
    assert payload.to_addresses == "repair@example.com"
    assert payload.cc_addresses == "cc@example.com"
    assert payload.subject == "Repair SN001"
    assert payload.text_body and "SN001" in payload.text_body
    assert payload.html_body and "<b>SN001</b>" in payload.html_body
    assert payload.sent_at is not None
    assert len(payload.attachments) == 2
    first = payload.attachments[0]
    assert first["file_name"] == "fault.txt"
    assert first["content_type"] == "text/plain"
    assert first["file_size"] == len(b"fault log")
    assert first["file_hash"] == hashlib.sha256(b"fault log").hexdigest()
    second = payload.attachments[1]
    assert second["is_inline"] is True
    assert second["content_id"] == "inline-image-1"


def test_attachment_blobs_from_eml_bytes_preserves_attachment_content_order() -> None:
    blobs = attachment_blobs_from_eml_bytes(_sample_eml())

    assert [blob["file_name"] for blob in blobs] == ["fault.txt", "fault.png"]
    assert [blob["content"] for blob in blobs] == [b"fault log", b"image-bytes"]


def test_payload_from_eml_bytes_rejects_missing_sender() -> None:
    message = EmailMessage()
    message["To"] = "repair@example.com"
    message.set_content("No sender")

    with pytest.raises(ValueError, match="EML_FROM_REQUIRED"):
        payload_from_eml_bytes(message.as_bytes(), mailbox_account="inbox@example.com")
