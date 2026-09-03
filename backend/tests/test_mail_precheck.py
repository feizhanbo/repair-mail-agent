from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models import Email, MailFetchRecord, ManualReviewTask
from app.schemas.business import EmailIngestRequest
from app.services import email_archival
from app.services.mail_ingress import persist_missing_message_id_anomaly
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
async def test_precheck_routes_missing_message_id_to_abnormal_flow_without_fabrication() -> None:
    raw_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    payload = _payload(subject="Repair SN001", text_body="Please repair SN001", message_id=None)
    payload.raw_eml_sha256 = raw_hash

    result = await precheck_email_payload(FakeSession(), payload)

    assert payload.message_id is None
    assert result.accepted is False
    assert result.status == "missing_message_id"
    assert result.reason == "MISSING_MESSAGE_ID"


@pytest.mark.anyio
async def test_manual_missing_message_id_preserves_raw_eml_and_creates_review_task(monkeypatch) -> None:
    class AnomalySession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.added = []

        def add(self, row) -> None:
            self.added.append(row)

        async def flush(self) -> None:
            for index, row in enumerate(self.added, start=1):
                if getattr(row, "id", None) is None:
                    row.id = index

    async def fake_upload(*_args, **_kwargs):
        return SimpleNamespace(id=77)

    monkeypatch.setattr(email_archival, "upload_bytes_to_oss", fake_upload)
    session = AnomalySession()
    payload = _payload(subject="Repair without RFC id", text_body="Please repair", message_id=None)
    raw = b"From: customer@example.com\r\nTo: repair@example.com\r\n\r\nPlease repair"

    record = await persist_missing_message_id_anomaly(
        session, payload=payload, raw_eml=raw, raw_file_name="missing.eml",
        source="eml_upload", user_id=7,
    )

    assert isinstance(record, MailFetchRecord)
    assert record.message_id is None
    assert record.fetch_status == "terminal_manual"
    assert record.raw_eml_oss_object_id == 77
    assert record.raw_retention_mode == "permanent"
    assert any(isinstance(row, ManualReviewTask) for row in session.added)


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


@pytest.mark.anyio
@pytest.mark.parametrize("recipient_field", ["to_addresses", "cc_addresses", "delivered_to_addresses", "x_original_to_addresses"])
async def test_imap_precheck_accepts_target_from_supported_recipient_headers(monkeypatch, recipient_field: str) -> None:
    monkeypatch.setattr("app.services.mail_precheck.settings.IMAP_USER", "rmatest1@accotest.com")
    payload = _payload(subject="Repair SN001", text_body="Please repair SN001")
    payload.to_addresses = "customer@example.com"
    setattr(payload, recipient_field, "RMA Test <rmatest1@accotest.com>")

    result = await precheck_email_payload(FakeSession(), payload, enforce_target_mailbox=True)

    assert result.accepted is True


@pytest.mark.anyio
async def test_imap_precheck_rejects_mail_sent_by_target_account(monkeypatch) -> None:
    monkeypatch.setattr("app.services.mail_precheck.settings.IMAP_USER", "rmatest1@accotest.com")
    payload = _payload(subject="Repair SN001", text_body="Please repair SN001")
    payload.from_address = "RMA Test <rmatest1@accotest.com>"
    payload.to_addresses = "rmatest2@accotest.com"

    result = await precheck_email_payload(FakeSession(), payload, enforce_target_mailbox=True)

    assert result.accepted is False
    assert result.status == "self_sent_mail_skipped"
    assert result.reason == "SELF_SENT_MAIL_SKIPPED"


@pytest.mark.anyio
async def test_precheck_rejects_configured_system_sender_for_manual_entry(monkeypatch) -> None:
    monkeypatch.setattr("app.services.mail_precheck.settings.SYSTEM_SENDER_ADDRESSES", ["rma-alias@example.com"])
    payload = _payload(subject="RMA sent", text_body="system reply")
    payload.from_address = "RMA Alias <rma-alias@example.com>"
    result = await precheck_email_payload(FakeSession(), payload)
    assert result.accepted is False
    assert result.status == "self_sent_mail_skipped"


@pytest.mark.anyio
async def test_imap_precheck_rejects_mail_not_delivered_to_target_account(monkeypatch) -> None:
    monkeypatch.setattr("app.services.mail_precheck.settings.IMAP_USER", "rmatest1@accotest.com")
    payload = _payload(subject="Repair SN001", text_body="Please repair SN001")
    payload.to_addresses = "someone-else@example.com"

    result = await precheck_email_payload(FakeSession(), payload, enforce_target_mailbox=True)

    assert result.accepted is False
    assert result.status == "recipient_not_target_mailbox"
    assert result.reason == "RECIPIENT_NOT_TARGET_MAILBOX"
