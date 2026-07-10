from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.ai_provider import AiExtractResponse, AiProviderError, AiReplyDraftResponse, DeepSeekProvider


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_provider(handler) -> DeepSeekProvider:
    return DeepSeekProvider(
        api_key="test-key",
        base_url="https://api.deepseek.example",
        model="deepseek-v4-flash",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.anyio
async def test_deepseek_provider_parses_valid_json() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url == "https://api.deepseek.example/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["model"] == "deepseek-v4-flash"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent_type": "new_repair",
                                    "extracted_fields": {"contact_email": "user@example.com"},
                                    "extracted_items": [{"line_no": 1, "sn": "SN001"}],
                                    "missing_fields": {},
                                    "conflict_fields": {},
                                    "confidence_score": 0.91,
                                    "field_confidences": {"sn": 0.9},
                                    "evidence": {"snippet": "SN001"},
                                }
                            )
                        }
                    }
                ]
            },
        )

    completion = await make_provider(handler).chat_json(
        messages=[{"role": "user", "content": "return JSON"}],
        response_model=AiExtractResponse,
    )

    assert completion.parsed.intent_type == "new_repair"
    assert completion.parsed.extracted_items[0]["sn"] == "SN001"
    assert completion.latency_ms >= 0
    assert completion.trace_id


@pytest.mark.anyio
async def test_deepseek_provider_rejects_empty_content() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    with pytest.raises(AiProviderError, match="AI_PROVIDER_EMPTY_CONTENT"):
        await make_provider(handler).chat_json(messages=[{"role": "user", "content": "return JSON"}], response_model=AiExtractResponse)


@pytest.mark.anyio
async def test_deepseek_provider_rejects_invalid_json_output() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    with pytest.raises(AiProviderError, match="AI_PROVIDER_OUTPUT_NOT_JSON"):
        await make_provider(handler).chat_json(messages=[{"role": "user", "content": "return JSON"}], response_model=AiExtractResponse)


def test_ai_extract_response_coerces_deepseek_business_variants() -> None:
    parsed = AiExtractResponse.model_validate(
        {
            "intent_type": "new_repair",
            "extracted_fields": {
                "applicant_name": "肖辉栋",
                "applicant_phone": "13046648575",
                "applicant_email": "huidong.xiao@jcetglobal.com",
                "company": "江苏长电科技股份有限公司",
            },
            "extracted_items": [
                {
                    "item_type": "QSC电源",
                    "sn": "GDH0D0038",
                    "fault_description": "输出0V",
                    "date": "2026-05-13",
                }
            ],
            "missing_fields": ["machine_model"],
            "conflict_fields": [],
            "confidence_score": 0.83,
        }
    )

    assert parsed.extracted_fields["contact_person"] == "肖辉栋"
    assert parsed.extracted_fields["contact_phone"] == "13046648575"
    assert parsed.extracted_fields["contact_email"] == "huidong.xiao@jcetglobal.com"
    assert parsed.extracted_fields["customer_name"] == "江苏长电科技股份有限公司"
    assert parsed.extracted_items[0]["material_name"] == "QSC电源"
    assert parsed.extracted_items[0]["failure_description"] == "输出0V"
    assert parsed.missing_fields == {"machine_model": "missing:machine_model"}
    assert parsed.conflict_fields == {}


def test_ai_extract_response_maps_fault_to_failure_description() -> None:
    parsed = AiExtractResponse.model_validate(
        {
            "extracted_items": [{"sn": "GDH0D0038", "fault": "输出0V"}],
            "confidence_score": 0.8,
        }
    )

    assert parsed.extracted_items[0]["failure_description"] == "输出0V"


def test_ai_reply_response_coerces_missing_fields_list() -> None:
    parsed = AiReplyDraftResponse.model_validate(
        {
            "subject": "请补充报修信息",
            "body": "请补充紧急程度。",
            "missing_fields": ["urgency_level"],
            "confidence_score": 0.7,
            "risk_level": "medium",
            "suggestions": "请客户补充紧急程度",
        }
    )

    assert parsed.missing_fields == {"urgency_level": "missing:urgency_level"}
    assert parsed.suggestions == ["请客户补充紧急程度"]


@pytest.mark.anyio
async def test_deepseek_provider_reports_http_errors() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "failed"}})

    with pytest.raises(AiProviderError, match="AI_PROVIDER_HTTP_500"):
        await make_provider(handler).chat_json(messages=[{"role": "user", "content": "return JSON"}], response_model=AiExtractResponse)


@pytest.mark.anyio
async def test_deepseek_provider_reports_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout", request=request)

    with pytest.raises(AiProviderError, match="AI_PROVIDER_TIMEOUT"):
        await make_provider(handler).chat_json(messages=[{"role": "user", "content": "return JSON"}], response_model=AiExtractResponse)
