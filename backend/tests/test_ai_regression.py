from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.integrations.ai_provider import (
    AiExtractResponse,
    AiReplyDraftResponse,
    DeepSeekProvider,
    _normalize_response_payload,
)
from app.services.ai import _key_result, _status_for


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_ai_extract_schema_accepts_sample_output() -> None:
    parsed = AiExtractResponse.model_validate(
        {
            "intent_type": "new_repair",
            "extracted_fields": {"contact_email": "customer@example.com", "problem_description": "设备无法开机"},
            "extracted_items": [{"line_no": 1, "sn": "SN202607040001", "failure_description": "无法开机"}],
            "missing_fields": {},
            "conflict_fields": {},
            "confidence_score": 0.86,
            "field_confidences": {"sn": 0.9, "problem_description": 0.8},
            "evidence": {"sn": "邮件正文第 2 行"},
        }
    )

    assert parsed.intent_type == "new_repair"
    assert parsed.extracted_items[0]["sn"] == "SN202607040001"
    assert _status_for(parsed, None) == "success"


def test_ai_extract_schema_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        AiExtractResponse.model_validate({"confidence_score": 1.2})


def test_deepseek_payload_normalization_handles_common_shape_drift() -> None:
    normalized = _normalize_response_payload(
        {
            "extracted_fields": None,
            "extracted_items": {"items": [{"sn": "SN001"}]},
            "missing_fields": ["contact_phone"],
            "conflict_fields": None,
            "confidence_score": 86,
            "field_confidences": {"sn": "92", "contact_phone": None},
            "confidence_reasons": "SN present",
            "original_evidence": "SN001",
        },
        AiExtractResponse,
    )
    parsed = AiExtractResponse.model_validate(normalized)
    assert parsed.extracted_items == [{"sn": "SN001"}]
    assert parsed.missing_fields == {"contact_phone": "需要补充"}
    assert parsed.confidence_score == 0.86
    assert parsed.field_confidences == {"sn": 0.92, "contact_phone": 0.0}
    assert parsed.confidence_reasons == ["SN present"]


def test_ai_reply_schema_accepts_sample_output() -> None:
    parsed = AiReplyDraftResponse.model_validate(
        {
            "subject": "请补充报修信息：RMA001",
            "body": "您好，请补充设备 SN 和故障现象。",
            "missing_fields": {"sn": "缺少设备 SN"},
            "confidence_score": 0.78,
            "risk_level": "low",
            "suggestions": ["人工审核后发送"],
        }
    )

    assert parsed.subject.startswith("请补充")
    assert _status_for(parsed, None) == "success"


def test_ai_log_key_result_keeps_summary_not_sensitive_values() -> None:
    parsed = AiExtractResponse.model_validate(
        {
            "intent_type": "new_repair",
            "extracted_fields": {"contact_email": "customer@example.com", "contact_phone": "13800138000"},
            "extracted_items": [{"sn": "SN-SENSITIVE-001"}],
            "missing_fields": {"mailing_address": "缺少邮寄地址"},
            "conflict_fields": {},
            "confidence_score": 0.91,
            "field_confidences": {},
            "evidence": {"snippet": "SN-SENSITIVE-001"},
        }
    )

    key_result = _key_result("field_extract", parsed)
    serialized = json.dumps(key_result, ensure_ascii=False)
    assert key_result == {
        "intent_type": "new_repair",
        "field_keys": ["contact_email", "contact_phone"],
        "item_count": 1,
        "missing_field_keys": ["mailing_address"],
        "conflict_field_keys": [],
    }
    assert "customer@example.com" not in serialized
    assert "13800138000" not in serialized
    assert "SN-SENSITIVE-001" not in serialized


@pytest.mark.anyio
async def test_deepseek_request_payload_does_not_persist_api_key() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-test-key"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent_type": "unknown",
                                    "extracted_fields": {},
                                    "extracted_items": [],
                                    "missing_fields": {},
                                    "conflict_fields": {},
                                    "confidence_score": 0.4,
                                    "field_confidences": {},
                                    "evidence": {},
                                }
                            )
                        }
                    }
                ]
            },
        )

    provider = DeepSeekProvider(
        api_key="secret-test-key",
        base_url="https://api.deepseek.example",
        model="deepseek-v4-flash",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    completion = await provider.chat_json(messages=[{"role": "user", "content": "return JSON"}], response_model=AiExtractResponse)

    assert "secret-test-key" not in json.dumps(completion.request_payload)
    assert _status_for(completion.parsed, None) == "low_confidence"
