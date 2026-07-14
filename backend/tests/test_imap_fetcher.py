from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from app.models import JobRunLog
from app.models.mail_fetch import MailFetchRecord
from app.services import imap_fetcher


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    def __init__(self, scalar_result=None) -> None:
        self.added = []
        self._next_id = 1
        self.scalar_result = scalar_result

    def add(self, instance) -> None:
        self.added.append(instance)

    async def scalar(self, _statement):
        return self.scalar_result

    async def flush(self) -> None:
        for instance in self.added:
            if getattr(instance, "id", None) is None:
                instance.id = self._next_id
                self._next_id += 1


class FakeImapClient:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.closed = False
        self.logged_out = False

    def select(self, folder_name: str, readonly: bool = True):
        assert folder_name == "INBOX"
        assert readonly is True
        return "OK", [b""]

    def uid(self, command: str, uid: str | None, *args):
        if command == "SEARCH":
            return "OK", [b"101"]
        if command == "FETCH":
            assert uid == "101"
            return "OK", [(b"BODY[]", self.raw)]
        raise AssertionError(command)

    def close(self) -> None:
        self.closed = True

    def logout(self) -> None:
        self.logged_out = True


def _raw_eml() -> bytes:
    message = EmailMessage()
    message["From"] = "Customer <customer@example.com>"
    message["To"] = "Repair <repair@example.com>"
    message["Subject"] = "Repair SN001"
    message["Message-ID"] = "<imap-101@example.com>"
    message.set_content("Please repair SN001")
    message.add_attachment(b"fault", maintype="text", subtype="plain", filename="fault.txt")
    return message.as_bytes()


def _raw_irrelevant_eml() -> bytes:
    message = EmailMessage()
    message["From"] = "News <news@example.com>"
    message["To"] = "Repair <repair@example.com>"
    message["Subject"] = "Newsletter"
    message["Message-ID"] = "<imap-newsletter@example.com>"
    message.set_content("unsubscribe from this newsletter")
    return message.as_bytes()


@pytest.mark.anyio
async def test_fetch_imap_emails_archives_eml_and_attachments_with_mocked_imap(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_eml()
    session = FakeSession()
    client = FakeImapClient(raw)
    uploaded: list[tuple[str, str | None]] = []
    ingested_payloads = []

    monkeypatch.setattr(imap_fetcher, "_connect", lambda: client)
    monkeypatch.setattr(imap_fetcher.settings, "IMAP_USER", "imap-test@example.com")

    async def fake_upload(_session, *, content: bytes, original_file_name: str | None, content_type: str | None, source_type: str, user_id: int | None = None):
        assert content
        uploaded.append((source_type, original_file_name))
        return SimpleNamespace(id=len(uploaded) + 100)

    async def fake_ingest(_session, *, payload, user_id: int | None, auto_parse: bool):
        ingested_payloads.append(payload)
        assert user_id == 7
        assert auto_parse is False
        return {"duplicate": False, "email": {"id": 77, "parse_status": "pending"}}

    monkeypatch.setattr(imap_fetcher, "upload_bytes_to_oss", fake_upload)
    monkeypatch.setattr(imap_fetcher.email_service, "ingest_email", fake_ingest)

    result = await imap_fetcher.fetch_imap_emails(session, limit=1, auto_parse=False, archive_to_oss=True, user_id=7)

    assert result["status"] == "success"
    assert result["processed_count"] == 1
    assert result["success_count"] == 1
    assert result["failed_count"] == 0
    assert uploaded == [("raw_eml", "imap-101.eml"), ("email_attachment", "fault.txt")]
    assert len(ingested_payloads) == 1
    payload = ingested_payloads[0]
    assert payload.raw_eml_oss_object_id == 101
    assert payload.attachments[0]["oss_object_id"] == 102
    assert payload.imap_uid == "101"
    assert payload.fetch_job_run_id == 1
    assert any(isinstance(item, JobRunLog) and item.status == "success" for item in session.added)
    assert any(isinstance(item, MailFetchRecord) and item.email_id == 77 for item in session.added)
    assert client.closed is True
    assert client.logged_out is True


@pytest.mark.anyio
async def test_fetch_imap_emails_skips_existing_uid_before_fetching_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = MailFetchRecord(
        mailbox_account="imap-test@example.com",
        folder_name="INBOX",
        imap_uid="101",
        message_id="<existing@example.com>",
        email_id=33,
        fetch_status="ingested",
    )
    existing.id = 9
    session = FakeSession(existing)
    client = FakeImapClient(_raw_eml())
    fetched_raw = False

    def fail_fetch_raw(_client, _uid: str) -> bytes:
        nonlocal fetched_raw
        fetched_raw = True
        raise AssertionError("raw fetch should not happen for duplicate UID")

    monkeypatch.setattr(imap_fetcher, "_connect", lambda: client)
    monkeypatch.setattr(imap_fetcher, "_uid_fetch_raw", fail_fetch_raw)
    monkeypatch.setattr(imap_fetcher.settings, "IMAP_USER", "imap-test@example.com")

    result = await imap_fetcher.fetch_imap_emails(session, limit=1, auto_parse=False, archive_to_oss=True, user_id=7)

    assert fetched_raw is False
    assert result["status"] == "success"
    assert result["success_count"] == 0
    assert result["skipped_count"] == 1
    assert result["fetched"][0]["fetch_status"] == "duplicate_uid_skipped"


@pytest.mark.anyio
async def test_fetch_imap_emails_skips_irrelevant_before_oss_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    client = FakeImapClient(_raw_irrelevant_eml())
    uploaded = False
    ingested = False

    async def fail_upload(*args, **kwargs):
        nonlocal uploaded
        uploaded = True
        raise AssertionError("irrelevant email should not be archived to OSS")

    async def fail_ingest(*args, **kwargs):
        nonlocal ingested
        ingested = True
        raise AssertionError("irrelevant email should not be ingested")

    monkeypatch.setattr(imap_fetcher, "_connect", lambda: client)
    monkeypatch.setattr(imap_fetcher.settings, "IMAP_USER", "imap-test@example.com")
    monkeypatch.setattr(imap_fetcher, "upload_bytes_to_oss", fail_upload)
    monkeypatch.setattr(imap_fetcher.email_service, "ingest_email", fail_ingest)

    result = await imap_fetcher.fetch_imap_emails(session, limit=1, auto_parse=False, archive_to_oss=True, user_id=7)

    assert uploaded is False
    assert ingested is False
    assert result["status"] == "success"
    assert result["success_count"] == 0
    assert result["skipped_count"] == 1
    assert result["fetched"][0]["fetch_status"] == "irrelevant_skipped"
    record = next(item for item in session.added if isinstance(item, MailFetchRecord))
    assert record.fetch_status == "irrelevant_skipped"
    assert record.email_id is None
