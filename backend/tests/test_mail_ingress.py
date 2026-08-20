from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.business import EmailIngestRequest
from app.services import mail_ingress


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for index, value in enumerate(self.added, start=1):
            if getattr(value, "id", None) is None:
                value.id = index

    async def get(self, _model, _object_id):
        return None


class DurableFakeSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def get(self, _model, object_id):
        return next((value for value in self.added if getattr(value, "id", None) == object_id), None)


def _payload() -> EmailIngestRequest:
    return EmailIngestRequest(
        mailbox_account="manual", folder_name="INBOX", message_id="<route@example.test>",
        from_address="customer@example.test", to_addresses="rma@example.test",
        subject="RMA", text_body="设备故障，需要维修。" * 10,
        raw_eml_sha256="a" * 64,
        attachments=[{"file_name": "fault.txt", "content_type": "text/plain", "file_size": 5}],
    )


def _decision(level: str, intent: str) -> SimpleNamespace:
    return SimpleNamespace(
        intent_type=intent, handling_level=level, confidence=0.95, reason_code="TEST",
        candidates=[], needs_attachment_content=False, evidence=["test"], classification_version="test-v1",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("level", "intent", "raw_count", "bundle_count", "minimal_count", "business_count", "job_count"),
    [
        ("auto_repair", "new_repair", 0, 1, 0, 1, 1),
        ("manual_rma_business", "warranty_status_inquiry", 1, 0, 1, 0, 0),
        ("lifecycle_only", "invoice", 0, 0, 0, 0, 0),
        ("unknown", "unknown", 1, 0, 1, 0, 0),
    ],
)
async def test_all_ingress_levels_apply_exact_persistence_policy(
    monkeypatch: pytest.MonkeyPatch,
    level: str,
    intent: str,
    raw_count: int,
    bundle_count: int,
    minimal_count: int,
    business_count: int,
    job_count: int,
) -> None:
    calls = {"raw": 0, "bundle": 0, "minimal": 0, "business": 0, "job": 0}
    monkeypatch.setattr(mail_ingress.email_service, "find_existing_thread_anchor", AsyncMock(return_value=None))
    monkeypatch.setattr(mail_ingress.email_service, "build_thread_classification_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(mail_ingress, "classify_mail", AsyncMock(return_value=_decision(level, intent)))

    async def raw(*_args, **_kwargs):
        calls["raw"] += 1

    async def bundle(*_args, **_kwargs):
        calls["bundle"] += 1

    async def minimal(*_args, **_kwargs):
        calls["minimal"] += 1
        return {"email": {"id": 20}, "manual_task_id": 30}

    async def business(*_args, **_kwargs):
        calls["business"] += 1
        return {"duplicate": False, "email": {"id": 40}, "rule_parse_result_id": 50}

    async def enqueue(*_args, **_kwargs):
        calls["job"] += 1
        return SimpleNamespace(id=60)

    monkeypatch.setattr(mail_ingress, "archive_raw_email", raw)
    monkeypatch.setattr(mail_ingress, "archive_email_bundle", bundle)
    monkeypatch.setattr(mail_ingress.email_service, "ingest_minimal_email", minimal)
    monkeypatch.setattr(mail_ingress.email_service, "ingest_email", business)
    monkeypatch.setattr(mail_ingress, "enqueue_job", enqueue)

    result = await mail_ingress.process_preclassified_ingress(
        FakeSession(), payload=_payload(), raw_eml=b"eml", raw_file_name="mail.eml",
        attachment_blobs=[{"file_name": "fault.txt", "content_type": "text/plain", "content": b"fault"}],
        source="manual_test", precheck=SimpleNamespace(rule_analysis=SimpleNamespace()),
        user_id=1, auto_parse=True,
    )
    assert calls == {
        "raw": raw_count, "bundle": bundle_count, "minimal": minimal_count,
        "business": business_count, "job": job_count,
    }
    assert result["classification"]["handling_level"] == level
    if level == "lifecycle_only":
        assert result["email"] is None
        assert result["fetch_status"] == "classified_third"


@pytest.mark.anyio
async def test_low_confidence_result_uses_transient_attachment_before_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    decisions = [_decision("unknown", "unknown"), _decision("auto_repair", "new_repair")]
    decisions[0].reason_code = "PRECLASSIFICATION_LOW_CONFIDENCE"
    classify = AsyncMock(side_effect=decisions)
    monkeypatch.setattr(mail_ingress.email_service, "find_existing_thread_anchor", AsyncMock(return_value=None))
    monkeypatch.setattr(mail_ingress.email_service, "build_thread_classification_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(mail_ingress, "classify_mail", classify)
    monkeypatch.setattr(mail_ingress, "archive_email_bundle", AsyncMock())
    monkeypatch.setattr(mail_ingress.email_service, "ingest_email", AsyncMock(return_value={
        "duplicate": False, "email": {"id": 40}, "rule_parse_result_id": 50,
    }))
    monkeypatch.setattr(mail_ingress, "enqueue_job", AsyncMock(return_value=SimpleNamespace(id=60)))
    await mail_ingress.process_preclassified_ingress(
        FakeSession(), payload=_payload(), raw_eml=b"eml", raw_file_name="mail.eml",
        attachment_blobs=[{"file_name": "fault.txt", "content_type": "text/plain", "content": b"SN001"}],
        source="manual_test", precheck=SimpleNamespace(rule_analysis=SimpleNamespace()),
        user_id=1, auto_parse=True,
    )
    assert classify.await_count == 2
    assert classify.await_args_list[1].kwargs["attachment_evidence"][0]["text"] == "SN001"


@pytest.mark.anyio
async def test_persistence_failure_keeps_durable_recovery_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DurableFakeSession()
    monkeypatch.setattr(mail_ingress.email_service, "find_existing_thread_anchor", AsyncMock(return_value=None))
    monkeypatch.setattr(mail_ingress.email_service, "build_thread_classification_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(
        mail_ingress, "classify_mail", AsyncMock(return_value=_decision("auto_repair", "new_repair"))
    )
    monkeypatch.setattr(mail_ingress, "archive_email_bundle", AsyncMock())
    monkeypatch.setattr(
        mail_ingress.email_service, "ingest_email", AsyncMock(side_effect=RuntimeError("database unavailable"))
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await mail_ingress.process_preclassified_ingress(
            session, payload=_payload(), raw_eml=b"eml", raw_file_name="mail.eml",
            attachment_blobs=[], source="manual_test",
            precheck=SimpleNamespace(rule_analysis=SimpleNamespace()), user_id=1, auto_parse=True,
        )

    record = session.added[0]
    assert session.rollbacks == 1
    assert session.commits >= 4
    assert record.fetch_status == "retry_wait"
    assert record.processing_stage == "persistence_failed"
    assert record.recovery_stage == "route_first"
    assert record.error_message == "RuntimeError"
