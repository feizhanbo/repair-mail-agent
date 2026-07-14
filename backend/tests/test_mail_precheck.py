from __future__ import annotations

import pytest

from app.models import Email
from app.schemas.business import EmailIngestRequest
from app.services.mail_precheck import precheck_email_payload


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    def __init__(self, duplicate: Email | None = None) -> None:
        self.duplicate = duplicate

    async def scalar(self, _statement):
        return self.duplicate


def _payload(*, subject: str, text_body: str, message_id: str | None = "<precheck@example.com>") -> EmailIngestRequest:
    return EmailIngestRequest(
        mailbox_account="manual",
        folder_name="INBOX",
        message_id=message_id,
        from_address="customer@example.com",
        to_addresses="repair@example.com",
        subject=subject,
        text_body=text_body,
    )


@pytest.mark.anyio
async def test_precheck_rejects_duplicate_message_id_before_oss_upload() -> None:
    duplicate = Email(mailbox_account="manual", message_id="<precheck@example.com>", from_address="customer@example.com")
    duplicate.id = 42

    result = await precheck_email_payload(FakeSession(duplicate), _payload(subject="Repair SN001", text_body="Please repair SN001"))

    assert result.accepted is False
    assert result.status == "duplicate_message_skipped"
    assert result.duplicate_email_id == 42


@pytest.mark.anyio
async def test_precheck_uses_raw_eml_hash_when_message_id_missing() -> None:
    raw_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    synthetic_message_id = "<raw-abcdef1234567890abcdef12@repair-mail-agent.local>"
    duplicate = Email(mailbox_account="manual", message_id=synthetic_message_id, from_address="customer@example.com")
    duplicate.id = 88
    payload = _payload(subject="Repair SN001", text_body="Please repair SN001", message_id=None)
    payload.raw_eml_sha256 = raw_hash

    result = await precheck_email_payload(FakeSession(duplicate), payload)

    assert payload.message_id == synthetic_message_id
    assert result.accepted is False
    assert result.status == "duplicate_message_skipped"
    assert result.duplicate_email_id == 88


@pytest.mark.anyio
async def test_precheck_skips_high_confidence_irrelevant_email() -> None:
    result = await precheck_email_payload(
        FakeSession(),
        _payload(subject="Newsletter", text_body="unsubscribe from this newsletter", message_id="<newsletter@example.com>"),
    )

    assert result.accepted is False
    assert result.status == "irrelevant_skipped"
    assert result.intent_type == "irrelevant"


@pytest.mark.anyio
async def test_precheck_keeps_unknown_email_for_review() -> None:
    result = await precheck_email_payload(
        FakeSession(),
        _payload(subject="Hello", text_body="Can you check this message?", message_id="<unknown@example.com>"),
    )

    assert result.accepted is True
    assert result.status == "accepted"
    assert result.intent_type == "unknown"
