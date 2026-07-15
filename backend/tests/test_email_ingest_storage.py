from __future__ import annotations

import pytest

from app.models import Email, EmailAttachment, EmailThread
from app.schemas.business import EmailIngestRequest
from app.services.emails import ingest_email


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    def __init__(self) -> None:
        self.added = []
        self._next_id = 1

    async def scalar(self, _statement):
        return None

    def add(self, instance) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        for instance in self.added:
            if getattr(instance, "id", None) is None:
                instance.id = self._next_id
                self._next_id += 1


@pytest.mark.anyio
async def test_ingest_email_persists_raw_eml_and_attachment_oss_ids() -> None:
    session = FakeSession()
    payload = EmailIngestRequest(
        mailbox_account="manual",
        folder_name="INBOX",
        message_id="<stored-oss@example.com>",
        from_address="customer@example.com",
        to_addresses="repair@example.com",
        subject="Repair",
        text_body="SN001 repair request",
        raw_eml_oss_object_id=101,
        attachments=[
            {
                "file_name": "fault.txt",
                "content_type": "text/plain",
                "file_size": 9,
                "file_hash": "a" * 64,
                "oss_object_id": 202,
            }
        ],
    )

    result = await ingest_email(session, payload=payload, user_id=7, auto_parse=False)

    emails = [item for item in session.added if isinstance(item, Email)]
    attachments = [item for item in session.added if isinstance(item, EmailAttachment)]
    threads = [item for item in session.added if isinstance(item, EmailThread)]
    assert result["duplicate"] is False
    assert len(threads) == 1
    assert len(emails) == 1
    assert emails[0].raw_eml_oss_object_id == 101
    assert emails[0].created_at is not None
    assert emails[0].updated_at is not None
    assert len(attachments) == 1
    assert attachments[0].oss_object_id == 202
    assert attachments[0].file_hash == "a" * 64
