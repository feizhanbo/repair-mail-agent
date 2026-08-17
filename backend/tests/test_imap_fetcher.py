from __future__ import annotations

from email.message import EmailMessage
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.models import JobRunLog
from app.models.mail_fetch import MailFetchRecord
from app.services import email_archival, imap_fetcher


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def configured_oss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imap_fetcher.settings, "OSS_ENDPOINT", "https://oss.example.test")
    monkeypatch.setattr(imap_fetcher.settings, "OSS_BUCKET", "test-bucket")
    monkeypatch.setattr(imap_fetcher.settings, "OSS_ACCESS_KEY", "test-access")
    monkeypatch.setattr(imap_fetcher.settings, "OSS_SECRET_KEY", "test-secret")


class FakeSession:
    def __init__(self, scalar_result=None) -> None:
        self.added = []
        self._next_id = 1
        self.scalar_result = scalar_result

    def add(self, instance) -> None:
        self.added.append(instance)

    async def scalar(self, _statement):
        return self.scalar_result

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

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
        self.uid_called = False

    def select(self, folder_name: str, readonly: bool = True):
        assert folder_name == "INBOX"
        assert readonly is True
        return "OK", [b""]

    def uid(self, command: str, uid: str | None, *args):
        self.uid_called = True
        if command == "SEARCH":
            return "OK", [b"101"]
        if command == "FETCH":
            assert uid == "101"
            return "OK", [(b"BODY[]", self.raw)]
        raise AssertionError(command)

    def response(self, code: str):
        assert code == "UIDVALIDITY"
        return "UIDVALIDITY", [b"777"]

    def close(self) -> None:
        self.closed = True

    def logout(self) -> None:
        self.logged_out = True


class MultiUidImapClient(FakeImapClient):
    def __init__(self, uids: list[str]) -> None:
        super().__init__(_raw_eml())
        self.uids = uids
        self.fetched_uids: list[str] = []

    def uid(self, command: str, uid: str | None, *args):
        if command == "SEARCH":
            return "OK", [" ".join(self.uids).encode()]
        if command == "FETCH":
            assert uid is not None
            self.fetched_uids.append(uid)
            message = EmailMessage()
            message["From"] = "Customer <customer@example.com>"
            message["To"] = "RMA Test <imap-test@example.com>"
            message["Subject"] = f"Repair SN{uid}"
            message["Message-ID"] = f"<imap-{uid}@example.com>"
            message.set_content(f"Please repair SN{uid}")
            return "OK", [(b"BODY[]", message.as_bytes())]
        raise AssertionError(command)


def test_uid_search_excludes_self_for_batch_but_not_exact_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    class SearchClient:
        def uid(self, *args):
            calls.append(args)
            if args[0] == "FETCH":
                return "OK", [(b"HEADER", b"Message-ID: <recover@example.com>\r\n\r\n")]
            return "OK", [b"101"]

    monkeypatch.setattr(imap_fetcher.settings, "IMAP_USER", "rmatest1@accotest.com")
    client = SearchClient()

    assert imap_fetcher._uid_search(client, message_id=None, unseen_only=True) == ["101"]
    assert calls[-1] == ("SEARCH", None, "UNSEEN", "NOT", "FROM", "rmatest1@accotest.com")
    assert imap_fetcher._uid_search(client, message_id="<recover@example.com>", unseen_only=False) == ["101"]
    assert calls[-2] == ("SEARCH", None, "HEADER", "Message-ID", "<recover@example.com>")


def test_uid_search_filters_reference_header_false_positive() -> None:
    class SearchClient:
        def uid(self, *args):
            if args[0] == "SEARCH":
                return "OK", [b"101 102"]
            uid = args[1]
            message_id = (
                b"<recover@example.com>" if uid == "101" else b"<supplement@example.com>"
            )
            return "OK", [(b"HEADER", b"Message-ID: " + message_id + b"\r\n\r\n")]

    assert imap_fetcher._uid_search(
        SearchClient(), message_id="<recover@example.com>", unseen_only=False
    ) == ["101"]


def _raw_eml() -> bytes:
    message = EmailMessage()
    message["From"] = "Customer <customer@example.com>"
    message["To"] = "RMA Test <imap-test@example.com>"
    message["Subject"] = "Repair SN001"
    message["Message-ID"] = "<imap-101@example.com>"
    message.set_content("Please repair SN001")
    message.add_attachment(b"fault", maintype="text", subtype="plain", filename="fault.txt")
    return message.as_bytes()


def _raw_irrelevant_eml() -> bytes:
    message = EmailMessage()
    message["From"] = "News <news@example.com>"
    message["To"] = "RMA Test <imap-test@example.com>"
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

    async def fake_ingest(_session, *, payload, user_id: int | None, auto_parse: bool, rule_analysis=None):
        ingested_payloads.append(payload)
        assert user_id == 7
        assert auto_parse is False
        assert rule_analysis is not None
        return {"duplicate": False, "email": {"id": 77, "parse_status": "pending"}}

    monkeypatch.setattr(email_archival, "upload_bytes_to_oss", fake_upload)
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
    assert any(isinstance(item, MailFetchRecord) and item.email_id == 77 and item.uid_validity == 777 for item in session.added)
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
    monkeypatch.setattr(email_archival, "upload_bytes_to_oss", fail_upload)
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


@pytest.mark.anyio
async def test_fetch_filters_processed_uids_before_applying_batch_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    client = MultiUidImapClient(["103", "101", "102"])
    ingested: list[str] = []

    async def fake_uid_precheck(_session, *, imap_uid: str, **_kwargs):
        if imap_uid == "101":
            return SimpleNamespace(status="duplicate_uid_skipped", to_dict=lambda: {"status": "duplicate_uid_skipped"})
        return None

    async def fake_archive(_session, *, payload, **_kwargs):
        return payload

    async def fake_ingest(_session, *, payload, **_kwargs):
        ingested.append(payload.imap_uid)
        return {"duplicate": False, "email": {"id": len(ingested), "parse_status": "pending"}}

    monkeypatch.setattr(imap_fetcher, "_connect", lambda: client)
    monkeypatch.setattr(imap_fetcher, "precheck_imap_uid", fake_uid_precheck)
    monkeypatch.setattr(imap_fetcher, "archive_email_bundle", fake_archive)
    monkeypatch.setattr(imap_fetcher.email_service, "ingest_email", fake_ingest)
    monkeypatch.setattr(imap_fetcher.settings, "IMAP_USER", "imap-test@example.com")

    result = await imap_fetcher.fetch_imap_emails(
        session,
        limit=2,
        auto_parse=False,
        archive_to_oss=True,
        user_id=7,
    )

    assert client.fetched_uids == ["102", "103"]
    assert ingested == ["102", "103"]
    assert result["processed_count"] == 2
    assert result["success_count"] == 2
    assert result["skipped_count"] == 1


@pytest.mark.anyio
async def test_locked_fetch_does_not_connect_when_another_fetch_owns_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()

    @asynccontextmanager
    async def busy_lock():
        yield False

    async def fail_fetch(*_args, **_kwargs):
        raise AssertionError("IMAP must not be called while the named lock is busy")

    monkeypatch.setattr(imap_fetcher, "imap_fetch_lock", busy_lock)
    monkeypatch.setattr(imap_fetcher, "fetch_imap_emails", fail_fetch)

    result = await imap_fetcher.run_imap_fetch_locked(session, limit=10)

    assert result["status"] == "skipped_busy"
    assert result["processed_count"] == 0


@pytest.mark.anyio
async def test_background_job_retries_instead_of_reporting_success_when_lock_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()

    @asynccontextmanager
    async def busy_lock():
        yield False

    monkeypatch.setattr(imap_fetcher, "imap_fetch_lock", busy_lock)

    with pytest.raises(imap_fetcher.ImapFetchError, match="IMAP_FETCH_BUSY"):
        await imap_fetcher.run_imap_fetch_locked(session, limit=10, busy_is_error=True)


@pytest.mark.anyio
async def test_imap_preflight_is_read_only_and_does_not_search_or_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeImapClient(_raw_eml())
    monkeypatch.setattr(imap_fetcher, "_connect", lambda: client)
    monkeypatch.setattr(imap_fetcher.settings, "IMAP_USER", "rmatest1@accotest.com")

    result = await imap_fetcher.preflight_imap(folder_name="INBOX")

    assert result["status"] == "ready"
    assert result["uid_validity"] == 777
    assert result["read_only"] is True
    assert result["messages_downloaded"] == 0
    assert result["flags_changed"] is False
    assert client.uid_called is False
    assert client.closed is True
    assert client.logged_out is True


@pytest.mark.anyio
async def test_oss_is_validated_before_imap_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    connected = False

    def fail_connect():
        nonlocal connected
        connected = True
        raise AssertionError("IMAP must not be contacted when OSS is unavailable")

    monkeypatch.setattr(imap_fetcher, "_connect", fail_connect)
    monkeypatch.setattr(imap_fetcher.settings, "OSS_SECRET_KEY", "")

    with pytest.raises(imap_fetcher.ImapConfigurationError, match="OSS_NOT_CONFIGURED"):
        await imap_fetcher.preflight_imap(folder_name="INBOX")
    assert connected is False


@pytest.mark.anyio
async def test_retry_success_updates_existing_uidvalidity_record(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = MailFetchRecord(
        mailbox_account="imap-test@example.com",
        folder_name="INBOX",
        uid_validity=777,
        imap_uid="101",
        message_id="",
        fetch_status="retry_wait",
        attempt_count=1,
    )
    existing.id = 91
    session = FakeSession(existing)
    client = FakeImapClient(_raw_eml())

    async def allow_uid(*_args, **_kwargs):
        return None

    async def accept_payload(*_args, **_kwargs):
        return SimpleNamespace(accepted=True, rule_analysis=SimpleNamespace(), message_id="<imap-101@example.com>")

    async def fake_archive(*_args, **_kwargs):
        return None

    async def fake_ingest(*_args, **_kwargs):
        return {"duplicate": False, "email": {"id": 77, "parse_status": "pending"}}

    monkeypatch.setattr(imap_fetcher, "_connect", lambda: client)
    monkeypatch.setattr(imap_fetcher, "precheck_imap_uid", allow_uid)
    monkeypatch.setattr(imap_fetcher, "precheck_email_payload", accept_payload)
    monkeypatch.setattr(imap_fetcher, "archive_email_bundle", fake_archive)
    monkeypatch.setattr(imap_fetcher.email_service, "ingest_email", fake_ingest)
    monkeypatch.setattr(imap_fetcher.settings, "IMAP_USER", "imap-test@example.com")

    result = await imap_fetcher.fetch_imap_emails(session, limit=1, auto_parse=False, archive_to_oss=True, user_id=7)

    assert result["status"] == "success"
    assert existing.fetch_status == "ingested"
    assert existing.email_id == 77
    assert existing.attempt_count == 2
    assert existing.next_retry_at is None
    assert [item for item in session.added if isinstance(item, MailFetchRecord)] == []


@pytest.mark.anyio
async def test_due_seen_retry_uid_is_merged_with_unseen_search(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    client = MultiUidImapClient(["101"])
    ingested: list[str] = []

    async def due_retries(*_args, **_kwargs):
        return ["99"]

    async def allow_uid(*_args, **_kwargs):
        return None

    async def fake_archive(*_args, **_kwargs):
        return None

    async def fake_ingest(_session, *, payload, **_kwargs):
        ingested.append(payload.imap_uid)
        return {"duplicate": False, "email": {"id": len(ingested), "parse_status": "pending"}}

    monkeypatch.setattr(imap_fetcher, "_connect", lambda: client)
    monkeypatch.setattr(imap_fetcher, "_due_retry_uids", due_retries)
    monkeypatch.setattr(imap_fetcher, "precheck_imap_uid", allow_uid)
    monkeypatch.setattr(imap_fetcher, "archive_email_bundle", fake_archive)
    monkeypatch.setattr(imap_fetcher.email_service, "ingest_email", fake_ingest)
    monkeypatch.setattr(imap_fetcher.settings, "IMAP_USER", "imap-test@example.com")

    result = await imap_fetcher.fetch_imap_emails(
        session,
        limit=2,
        unseen_only=True,
        auto_parse=False,
        archive_to_oss=True,
        user_id=7,
    )

    assert result["processed_count"] == 2
    assert ingested == ["99", "101"]
