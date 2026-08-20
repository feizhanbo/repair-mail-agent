from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.integrations.ai_provider import AiProviderError
from app.schemas.business import EmailIngestRequest
from app.services import mail_preclassification
from app.integrations.llm_gateway import LlmTask


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _payload() -> EmailIngestRequest:
    return EmailIngestRequest(
        mailbox_account="rma@example.com",
        message_id="<one@example.com>",
        from_address="customer@example.com",
        subject="设备报修",
        text_body="设备故障，请安排维修。",
        attachments=[{"file_name": "photo.jpg", "content_type": "image/jpeg", "file_size": 123}],
    )


@pytest.mark.anyio
async def test_classification_returns_canonical_level(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gateway(**kwargs):
        model = kwargs["response_model"]
        return SimpleNamespace(parsed=model(
            intent="new_repair", confidence=0.93, candidates=[], reason_code="SUBJECT_AND_BODY_MATCH",
            needs_attachment_content=False, evidence=["subject"],
        ))

    monkeypatch.setattr(mail_preclassification, "invoke_structured", fake_gateway)
    decision = await mail_preclassification.classify_mail(_payload())
    assert decision.intent_type == "new_repair"
    assert decision.handling_level == "auto_repair"


@pytest.mark.anyio
async def test_low_confidence_is_forced_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAIL_PRECLASSIFICATION_MIN_CONFIDENCE", 0.8)

    async def fake_gateway(**kwargs):
        model = kwargs["response_model"]
        return SimpleNamespace(parsed=model(intent="new_repair", confidence=0.5, reason_code="WEAK"))

    monkeypatch.setattr(mail_preclassification, "invoke_structured", fake_gateway)
    decision = await mail_preclassification.classify_mail(_payload())
    assert decision.handling_level == "unknown"
    assert decision.reason_code == "PRECLASSIFICATION_LOW_CONFIDENCE"


@pytest.mark.anyio
async def test_provider_failure_is_forced_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gateway(**kwargs):
        del kwargs
        raise AiProviderError("AI_PROVIDER_TIMEOUT")

    monkeypatch.setattr(mail_preclassification, "invoke_structured", fake_gateway)
    decision = await mail_preclassification.classify_mail(_payload())
    assert decision.intent_type == "unknown"
    assert decision.reason_code == "PRECLASSIFICATION_PROVIDER_FAILED"


def test_transient_attachment_evidence_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAIL_PRECLASSIFICATION_ATTACHMENT_MAX_BYTES", 8)
    evidence = mail_preclassification.transient_attachment_evidence([
        {"file_name": "small.txt", "content_type": "text/plain", "content": b"SN001"},
        {"file_name": "large.txt", "content_type": "text/plain", "content": b"012345678"},
        {"file_name": "fault.png", "content_type": "image/png", "content": b"png"},
    ])
    assert [item["file_name"] for item in evidence] == ["small.txt", "fault.png"]
    assert evidence[0]["text"] == "SN001"
    assert evidence[1]["data_url"].startswith("data:image/png;base64,")


def test_preclassification_context_uses_real_latest_reply_and_thread_summary() -> None:
    payload = _payload()
    payload.text_body = "补充 SN001\n\nOn Wed, Customer wrote:\n历史邮件中的 SN999"
    context = mail_preclassification._context(
        payload,
        thread_summary={"thread_id": 9, "ticket_status": "need_customer_info"},
    )
    assert context["latest_reply_segment"] == "补充 SN001"
    assert "SN999" in context["body"]
    assert context["thread_summary"]["thread_id"] == 9


@pytest.mark.anyio
async def test_visual_evidence_routes_to_qwen_vl(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def fake_gateway(**kwargs):
        seen["task"] = kwargs["task"]
        model = kwargs["response_model"]
        return SimpleNamespace(parsed=model(
            intent="new_repair", confidence=0.91, reason_code="IMAGE_EVIDENCE", evidence=["image"],
        ))

    monkeypatch.setattr(mail_preclassification, "invoke_structured", fake_gateway)
    decision = await mail_preclassification.classify_mail(
        _payload(),
        attachment_evidence=[{"file_name": "fault.png", "data_url": "data:image/png;base64,cG5n"}],
    )
    assert seen["task"] == LlmTask.ATTACHMENT_VISUAL_PARSE
    assert decision.intent_type == "new_repair"
