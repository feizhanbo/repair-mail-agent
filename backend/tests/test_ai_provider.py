from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from app.integrations.ai_provider import AiExtractResponse, AiProviderError, DeepSeekProvider
from app.integrations.qwen_provider import QwenProvider


class RequiredQwenOutput(BaseModel):
    file_type: str


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
                            "role": "assistant",
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
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": ""}}]})

    with pytest.raises(AiProviderError, match="AI_PROVIDER_EMPTY_CONTENT"):
        await make_provider(handler).chat_json(messages=[{"role": "user", "content": "return JSON"}], response_model=AiExtractResponse)


@pytest.mark.anyio
async def test_deepseek_provider_rejects_invalid_json_output() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "not-json"}}]})

    with pytest.raises(AiProviderError, match="AI_PROVIDER_OUTPUT_NOT_JSON"):
        await make_provider(handler).chat_json(messages=[{"role": "user", "content": "return JSON"}], response_model=AiExtractResponse)


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


@pytest.mark.anyio
async def test_qwen_schema_failure_keeps_request_and_response_for_detail_log() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": json.dumps({"unexpected": True})}}],
                "usage": {"total_tokens": 7},
            },
        )

    provider = QwenProvider(
        api_key="test-key",
        base_url="https://qwen.example",
        model="qwen-plus",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AiProviderError, match="QWEN_PROVIDER_OUTPUT_SCHEMA_INVALID") as exc_info:
        await provider.chat_json(
            messages=[{"role": "user", "content": "full semantic prompt"}],
            response_model=RequiredQwenOutput,
        )

    error = exc_info.value
    assert error.request_payload["messages"][0]["content"] == "full semantic prompt"
    assert error.response_payload["usage"]["total_tokens"] == 7
    assert error.raw_output == json.dumps({"unexpected": True})
    assert error.trace_id
