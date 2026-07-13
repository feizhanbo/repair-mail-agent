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
    def __init__(self) -> None:
        self.added = []
        self._next_id = 1

    def add(self, instance) -> None:
        self.added.append(instance)

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
