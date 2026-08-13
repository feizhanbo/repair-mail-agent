from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from app.ai.gateway import LangChainGatewayError, LangChainStructuredGateway
from app.ai.models import ModelSpec


class ResultSchema(BaseModel):
    value: str


class FakeStructuredChatModel:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.seen_schema: dict[str, Any] | None = None
        self.seen_method: str | None = None

    def with_structured_output(self, schema, *, method: str, include_raw: bool):
        assert include_raw is True
        self.seen_schema = schema
        self.seen_method = method
        return RunnableLambda(lambda _messages: self.result)


def _spec() -> ModelSpec:
    return ModelSpec(
        provider="fake",
        model="fake-model",
        api_key="fake-key",
        base_url="https://fake.invalid/v1",
        timeout_seconds=1,
        structured_output_method="json_mode",
    )


@pytest.mark.anyio
async def test_gateway_uses_langchain_structured_output_and_pydantic_validation() -> None:
    raw = AIMessage(
        content='{"value":"ok"}',
        usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    )
    model = FakeStructuredChatModel(
        {"raw": raw, "parsed": {"value": "ok"}, "parsing_error": None}
    )

    completion = await LangChainStructuredGateway(model, _spec()).invoke(
        messages=[{"role": "user", "content": "structured please"}],
        response_model=ResultSchema,
    )

    assert completion.parsed == ResultSchema(value="ok")
    assert completion.response_payload["usage"]["total_tokens"] == 5
    assert model.seen_method == "json_mode"
    assert model.seen_schema and model.seen_schema["properties"]["value"]


@pytest.mark.anyio
async def test_gateway_preserves_invalid_output_evidence() -> None:
    model = FakeStructuredChatModel(
        {
            "raw": AIMessage(content="not-json"),
            "parsed": None,
            "parsing_error": ValueError("invalid json"),
        }
    )

    with pytest.raises(LangChainGatewayError, match="OUTPUT_NOT_JSON") as exc_info:
        await LangChainStructuredGateway(model, _spec()).invoke(
            messages=[{"role": "user", "content": "structured please"}],
            response_model=ResultSchema,
        )

    assert exc_info.value.raw_output == "not-json"
    assert exc_info.value.request_payload["model"] == "fake-model"
