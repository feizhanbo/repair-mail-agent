from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import Email, EmailAttachment, JobRunLog, OssObject, ParseResult, RepairTicket
from app.api.deps import CurrentUser
from app.api.v1.jobs import _EXPORT_FILTERS, _can_access_job
from app.schemas.business import EmailIngestRequest
from app.services import email_archival
from app.services.email_archival import EmailArchivalError, archive_email_bundle, validate_archive_bundle
from app.services.emails import _parse_requires_manual, ingest_email
from app.services.common import utcnow
from app.services.jobs import _job_error_is_retryable, enqueue_job, recover_stale_jobs
from app.services.logging_safety import safe_error_code, sanitize_log_payload
from app.services import storage
from app.services.storage import StorageUploadError, _object_key, normalized_content_type


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    def __init__(self) -> None:
        self.added = []
        self._next_id = 1
        self.scalar_results = []

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = self._next_id
                self._next_id += 1

    async def scalar(self, _statement):
        return self.scalar_results.pop(0) if self.scalar_results else None


def _payload() -> EmailIngestRequest:
    return EmailIngestRequest(
        mailbox_account="manual",
        message_id="<archive@example.com>",
        from_address="sender@example.com",
        subject="Repair",
        text_body="repair request",
        attachments=[{"file_name": "fault.txt", "content_type": "text/plain"}],
    )


def test_oss_object_key_is_content_stable_and_date_free() -> None:
    first = _object_key(source_type="raw_eml", original_file_name="mail.eml", sha256_hash="a" * 64)
    second = _object_key(source_type="raw_eml", original_file_name="mail.eml", sha256_hash="a" * 64)
    assert first == second
    assert first == f"raw_eml/aa/{'a' * 64}-mail.eml"


def test_generic_pdf_content_type_is_inferred_from_file_name() -> None:
    assert normalized_content_type("repair-form.pdf", "application/octet-stream") == "application/pdf"


@pytest.mark.anyio
async def test_existing_oss_object_is_reuploaded_when_content_type_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    content = b"%PDF-1.7"
    existing = OssObject(
        bucket="test-bucket",
        endpoint="test-endpoint",
        object_key=_object_key(
            source_type="email_attachment",
            original_file_name="repair-form.pdf",
            sha256_hash=__import__("hashlib").sha256(content).hexdigest(),
        ),
        original_file_name="repair-form.pdf",
        safe_file_name="repair-form.pdf",
        content_type="application/octet-stream",
        file_size=len(content),
        sha256_hash=__import__("hashlib").sha256(content).hexdigest(),
        source_type="email_attachment",
        upload_status="success",
    )
    session.scalar_results.append(existing)
    calls = {"head": 0, "put": 0, "headers": None}

    class Bucket:
        def object_exists(self, _key):
            calls["head"] += 1
            return True

        def put_object(self, _key, _content, *, headers=None):
            calls["put"] += 1
            calls["headers"] = headers
            return SimpleNamespace(etag="etag")

    monkeypatch.setattr(storage, "_oss_configured", lambda: True)
    monkeypatch.setattr(storage, "_build_bucket", lambda **_kwargs: Bucket())
    monkeypatch.setattr(storage.settings, "OSS_BUCKET", "test-bucket")
    monkeypatch.setattr(storage.settings, "OSS_ENDPOINT", "test-endpoint")

    result = await storage.upload_bytes_to_oss(
        session,
        content=content,
        original_file_name="repair-form.pdf",
        content_type="application/octet-stream",
        source_type="email_attachment",
    )

    assert result is existing
    assert calls == {"head": 0, "put": 1, "headers": {"Content-Type": "application/pdf"}}
    assert existing.content_type == "application/pdf"
    assert existing.upload_status == "success"


def test_log_payload_redacts_secrets_content_and_signed_query() -> None:
    payload = sanitize_log_payload(
        {
            "api_key": "secret-value",
            "input_tokens": 321,
            "body": "customer mail body",
            "reason_code": "INVALID_CREDENTIALS",
            "url": "https://oss.example/file?OSSAccessKeyId=abc&Signature=def",
        }
    )
    assert payload["api_key"] == "[REDACTED]"
    assert payload["input_tokens"] == 321
    assert payload["body"]["redacted"] is True
    assert payload["reason_code"] == "INVALID_CREDENTIALS"
    serialized = str(payload)
    assert "customer mail body" not in serialized
    assert "secret-value" not in serialized
    assert "Signature=def" not in serialized


def test_async_sn_export_filters_match_service_contract() -> None:
    assert _EXPORT_FILTERS["sn_assets"] == {"keyword", "sn", "customer", "material", "asset_status"}


def test_job_error_classification_separates_deterministic_failures() -> None:
    assert safe_error_code(HTTPException(status_code=404, detail="EMAIL_NOT_FOUND")) == "EMAIL_NOT_FOUND"
    assert _job_error_is_retryable("EMAIL_NOT_FOUND") is False
    assert _job_error_is_retryable("SMTP_SEND_FAILED_UNCERTAIN") is False
    assert _job_error_is_retryable("HTTP_503") is True


def test_mysql_server_defaults_are_eagerly_loaded_for_all_models() -> None:
    assert Email.__mapper__.eager_defaults is True
    assert RepairTicket.__mapper__.eager_defaults is True


def test_job_access_is_limited_to_privileged_roles_or_creator() -> None:
    job = JobRunLog(job_name="email_parse", job_type="email_parse", metadata_json={"user_id": 7})
    def user(user_id: int, username: str, roles: list[str]) -> CurrentUser:
        return CurrentUser(
            id=user_id,
            username=username,
            real_name=username.title(),
            email=None,
            phone=None,
            department=None,
            status="active",
            roles=roles,
        )

    creator = user(7, "creator", ["operator"])
    other = user(8, "other", ["operator"])
    admin = user(9, "admin", ["admin"])
    assert _can_access_job(creator, job) is True
    assert _can_access_job(other, job) is False
    assert _can_access_job(admin, job) is True


def test_auto_apply_uses_dedicated_high_confidence_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.emails.settings.AUTO_APPLY_MIN_CONFIDENCE", 0.85)
    candidate = ParseResult(
        email_id=1,
        parser_type="deepseek",
        intent_type="new_repair",
        confidence_score=0.84,
        missing_fields={},
        conflict_fields={},
    )
    assert _parse_requires_manual(candidate, []) is True
    candidate.confidence_score = 0.85
    assert _parse_requires_manual(candidate, []) is False


def test_clear_incomplete_repair_uses_followup_path_below_auto_apply_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.emails.settings.AUTO_APPLY_MIN_CONFIDENCE", 0.85)
    monkeypatch.setattr("app.services.emails.settings.CONFIDENCE_THRESHOLD", 0.70)
    candidate = ParseResult(
        email_id=1,
        parser_type="deepseek",
        intent_type="new_repair",
        confidence_score=0.75,
        missing_fields={"contact_phone": "missing"},
        conflict_fields={},
    )
    assert _parse_requires_manual(candidate, []) is False
    candidate.confidence_score = 0.69
    assert _parse_requires_manual(candidate, []) is True


def test_skipped_engineering_archive_does_not_require_manual_review() -> None:
    candidate = ParseResult(
        email_id=1,
        parser_type="deepseek",
        intent_type="new_repair",
        confidence_score=0.95,
        missing_fields={},
        conflict_fields={},
    )
    attachment = EmailAttachment(
        email_id=1,
        file_name="self-check.zip",
        content_type="application/zip",
        parse_status="skipped",
        extracted_json={
            "attachment_role": "engineering_reference",
            "blocks_ticket_flow": False,
        },
    )

    assert _parse_requires_manual(candidate, [attachment]) is False


def test_prc_failure_only_blocks_when_customer_fields_are_still_missing() -> None:
    attachment = EmailAttachment(
        email_id=1,
        file_name="self-check.prc",
        content_type="application/octet-stream",
        parse_status="needs_manual_review",
        parse_error="PRC_TEXT_UNRECOGNIZED",
    )
    candidate = ParseResult(
        email_id=1,
        parser_type="deepseek",
        intent_type="new_repair",
        confidence_score=0.95,
        missing_fields={},
        conflict_fields={},
    )
    assert _parse_requires_manual(candidate, [attachment]) is False
    candidate.missing_fields = {"mailing_address": "required"}
    assert _parse_requires_manual(candidate, [attachment]) is True


@pytest.mark.anyio
async def test_stale_job_recovery_terminates_unlocked_orphan_without_replay() -> None:
    job = JobRunLog(
        id=170,
        job_name="imap_fetch_now",
        job_type="imap_fetch",
        status="running",
        started_at=utcnow() - timedelta(hours=2),
        locked_at=None,
        attempt_count=1,
        max_attempts=3,
    )

    class Rows:
        def scalars(self):
            return self

        def all(self):
            return [job]

    class RecoverySession:
        async def execute(self, _statement):
            return Rows()

    assert await recover_stale_jobs(RecoverySession()) == 1
    assert job.status == "failed"
    assert job.error_code == "JOB_ORPHANED_NO_LOCK"
    assert job.finished_at is not None


@pytest.mark.anyio
async def test_archive_bundle_maps_every_successful_object(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    payload = _payload()
    calls = []

    async def fake_upload(_session, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=100 + len(calls), upload_status="success")

    monkeypatch.setattr(email_archival, "upload_bytes_to_oss", fake_upload)
    result = await archive_email_bundle(
        session,
        payload=payload,
        raw_eml=b"raw eml",
        raw_file_name="mail.eml",
        attachment_blobs=[{"file_name": "fault.txt", "content_type": "text/plain", "content": b"fault"}],
        source="test",
    )

    assert result.raw_object_id == 101
    assert result.attachment_object_ids == [102]
    assert payload.raw_eml_oss_object_id == 101
    assert payload.attachments[0]["oss_object_id"] == 102
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("raw_eml", "blobs", "expected_code"),
    [
        (b"raw", [{"content": b"12345"}], "ATTACHMENT_ARCHIVE_TOO_LARGE"),
        (b"123456789", [], "EMAIL_ARCHIVE_TOO_LARGE"),
        (b"raw", [{"content": b"x"}, {"content": b"y"}], "TOO_MANY_ATTACHMENTS"),
    ],
)
def test_archive_hard_limits_remain_enforced(
    monkeypatch: pytest.MonkeyPatch,
    raw_eml: bytes,
    blobs: list[dict[str, bytes]],
    expected_code: str,
) -> None:
    monkeypatch.setattr(email_archival.settings, "ATTACHMENT_MAX_ARCHIVE_BYTES", 4)
    monkeypatch.setattr(email_archival.settings, "EMAIL_MAX_ARCHIVE_BYTES", 8)
    monkeypatch.setattr(email_archival.settings, "EMAIL_MAX_ATTACHMENTS", 1)

    with pytest.raises(EmailArchivalError) as exc_info:
        validate_archive_bundle(raw_eml, blobs)

    assert exc_info.value.code == expected_code
    assert exc_info.value.stage == "validate"


@pytest.mark.anyio
async def test_archive_failure_stops_before_formal_email(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    payload = _payload()
    call_count = 0

    async def failing_upload(_session, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise StorageUploadError("OSS_UPLOAD_FAILED")
        return SimpleNamespace(id=101, upload_status="success")

    monkeypatch.setattr(email_archival, "upload_bytes_to_oss", failing_upload)
    with pytest.raises(EmailArchivalError, match="OSS_ARCHIVAL_FAILED"):
        await archive_email_bundle(
            session,
            payload=payload,
            raw_eml=b"raw eml",
            raw_file_name="mail.eml",
            attachment_blobs=[{"file_name": "fault.txt", "content_type": "text/plain", "content": b"fault"}],
            source="test",
        )
    assert not any(isinstance(row, Email) for row in session.added)


@pytest.mark.anyio
async def test_formal_ingest_requires_complete_oss_references() -> None:
    session = FakeSession()
    with pytest.raises(HTTPException) as exc_info:
        await ingest_email(session, payload=_payload(), auto_parse=False)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "OSS_ARCHIVAL_REQUIRED"


@pytest.mark.anyio
async def test_enqueue_job_reuses_idempotency_key() -> None:
    session = FakeSession()
    first = await enqueue_job(
        session,
        job_type="email_parse",
        resource_type="email",
        resource_id=8,
        idempotency_key="email_parse:8:initial",
        metadata={"body": "must not persist"},
    )
    session.scalar_results.append(first)
    second = await enqueue_job(
        session,
        job_type="email_parse",
        resource_type="email",
        resource_id=8,
        idempotency_key="email_parse:8:initial",
    )
    assert first is second
    assert first.status == "queued"
    assert first.processed_count == 0
    assert first.success_count == 0
    assert first.failed_count == 0
    assert first.attempt_count == 0
    assert first.created_at is not None
    assert first.updated_at is not None
    assert first.metadata_json["body"]["redacted"] is True
    assert len([row for row in session.added if isinstance(row, JobRunLog)]) == 1
