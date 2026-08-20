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
    assert routes["mail_classification"]["primary"]["profile"] == "deepseek"
    assert routes["attachment_visual_parse"]["primary"]["profile"] == "qwen"
    assert "deepseek-secret" not in serialized
    assert "qwen-secret" not in serialized


@pytest.mark.anyio
async def test_transient_primary_failure_uses_configured_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_API_KEY", "deepseek-key")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    seen: list[str] = []

    class FakeChat:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

        def with_structured_output(self, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            seen.append(self.model)
            if self.model == "deepseek-chat":
                raise TimeoutError("timeout")
            raw = SimpleNamespace(content=json.dumps({"value": "fallback"}), response_metadata={}, usage_metadata={}, id="ok")
            return {"raw": raw, "parsed": {"value": "fallback"}, "parsing_error": None}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(llm_gateway, "ChatOpenAI", FakeChat)
    monkeypatch.setattr(llm_gateway.asyncio, "sleep", no_sleep)
    completion = await llm_gateway.invoke_structured(
        task=LlmTask.MAIL_CLASSIFICATION,
        messages=[{"role": "user", "content": "json"}],
        response_model=RequiredOutput,
    )
    assert completion.parsed.value == "fallback"
    assert completion.fallback_used is True
    assert completion.provider_name == "qwen"
    assert seen == ["deepseek-chat"] * 3 + ["qwen-plus"]


@pytest.mark.anyio
async def test_invalid_json_does_not_cross_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_API_KEY", "deepseek-key")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    seen: list[str] = []

    class FakeChat:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

        def with_structured_output(self, **_kwargs):
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
    assert seen == ["deepseek-chat"]
