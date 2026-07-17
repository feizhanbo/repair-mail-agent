from __future__ import annotations

from datetime import datetime

from app.models import Email, EmailAttachment
from app.services.emails import attachment_file_size_kb, serialize_attachment


def test_attachment_file_size_kb_uses_ceiling_with_minimum_one() -> None:
    assert attachment_file_size_kb(None) is None
    assert attachment_file_size_kb(1) == 1
    assert attachment_file_size_kb(1024) == 1
    assert attachment_file_size_kb(1025) == 2


def test_serialize_attachment_uses_email_sent_time_and_kb_size() -> None:
    sent_at = datetime(2026, 7, 16, 10, 30)
    email = Email(
        mailbox_account="manual-eml",
        message_id="<attachment@example.com>",
        from_address="customer@example.com",
        sent_at=sent_at,
    )
    attachment = EmailAttachment(
        email_id=1,
        file_name="fault.pdf",
        file_size=1025,
        parse_status="parsed",
    )

    data = serialize_attachment(attachment, email)

    assert data["sent_at"] == "2026-07-16T10:30:00"
    assert data["file_size_kb"] == 2
