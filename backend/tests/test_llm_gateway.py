from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.config import settings
from app.integrations import llm_gateway
from app.integrations.ai_provider import AiProviderError
from app.integrations.llm_gateway import LlmTask


class RequiredOutput(BaseModel):
    value: str


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_public_routes_are_task_specific_and_do_not_expose_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_API_KEY", "deepseek-secret")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-secret")
    routes = llm_gateway.public_llm_routes()
    serialized = json.dumps(routes)
    assert routes["mail_classification"]["primary"] == {"profile": "qwen", "model": "qwen3.7-plus"}
    assert routes["mail_classification"]["fallback"] == {"profile": "qwen", "model": "qwen3.8-flash"}
    assert routes["attachment_text_parse"]["primary"]["model"] == "qwen3.7-plus"
    assert routes["reply_draft"]["primary"]["model"] == "qwen3.7-plus"
    assert routes["mail_classification"]["structured_output_method"] == "json_schema"
    assert routes["attachment_visual_parse"]["structured_output_method"] == "json_mode"
    assert routes["attachment_visual_parse"]["primary"]["profile"] == "qwen"
    assert "deepseek-secret" not in serialized
    assert "qwen-secret" not in serialized


@pytest.mark.anyio
async def test_transient_classification_failure_retries_then_uses_flash_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_API_KEY", "deepseek-key")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    seen: list[str] = []
    max_tokens_seen: list[int | None] = []

    class FakeChat:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            max_tokens_seen.append(kwargs.get("max_tokens"))

        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            seen.append(self.model)
            raise TimeoutError("timeout")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(llm_gateway, "ChatOpenAI", FakeChat)
    monkeypatch.setattr(llm_gateway.asyncio, "sleep", no_sleep)
    with pytest.raises(AiProviderError):
        await llm_gateway.invoke_structured(
            task=LlmTask.MAIL_CLASSIFICATION,
            messages=[{"role": "user", "content": "json"}],
            response_model=RequiredOutput,
        )
    assert seen == ["qwen3.7-plus"] * 3 + ["qwen3.8-flash"] * 3
    assert max_tokens_seen == [4096] * 6


@pytest.mark.anyio
async def test_invalid_json_retries_and_uses_schema_capable_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_API_KEY", "deepseek-key")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    seen: list[str] = []

    class FakeChat:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            seen.append(self.model)
            raw = SimpleNamespace(content="not-json", response_metadata={}, usage_metadata={}, id="bad")
            return {"raw": raw, "parsed": None, "parsing_error": None}

    monkeypatch.setattr(llm_gateway, "ChatOpenAI", FakeChat)
    with pytest.raises(AiProviderError, match="OUTPUT_NOT_JSON"):
        await llm_gateway.invoke_structured(
            task=LlmTask.MAIL_CLASSIFICATION,
            messages=[{"role": "user", "content": "json"}],
            response_model=RequiredOutput,
        )
    assert seen == ["qwen3.7-plus"] * 3 + ["qwen3.8-flash"] * 3
