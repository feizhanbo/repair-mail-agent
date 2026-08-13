from __future__ import annotations

import os

import pytest
from pydantic import BaseModel

from app.config import settings
from app.integrations.ai_provider import DeepSeekProvider
from app.integrations.qwen_provider import QwenProvider


SYNTHETIC_WHITE_PIXEL_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z2S8AAAAASUVORK5CYII="
)


class LiveStructuredProbe(BaseModel):
    marker: str


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _configured_secret(value: str) -> bool:
    normalized = value.strip()
    return bool(normalized and normalized not in {"<CHANGE_ME>", "change-me"})


@pytest.mark.anyio
async def test_real_deepseek_qwen_and_qwen_vl_structured_output() -> None:
    if os.getenv("RUN_REAL_AI_INTEGRATION_TESTS") != "1":
        pytest.skip("real paid model integration is explicitly opt-in")
    missing = [
        name
        for name, configured in {
            "AI_API_KEY": _configured_secret(settings.AI_API_KEY),
            "AI_MODEL": bool(settings.AI_MODEL.strip()),
            "QWEN_API_KEY": _configured_secret(settings.QWEN_API_KEY),
            "QWEN_MODEL": bool(settings.QWEN_MODEL.strip()),
            "QWEN_VL_MODEL": bool(settings.QWEN_VL_MODEL.strip()),
        }.items()
        if not configured
    ]
    assert not missing, f"missing live AI integration settings: {', '.join(missing)}"

    instruction = (
        "This is a synthetic integration probe. Return only the structured field "
        "marker with the exact value LANGCHAIN_OK."
    )
    deepseek = DeepSeekProvider(
        api_key=settings.AI_API_KEY,
        base_url=settings.AI_BASE_URL,
        model=settings.AI_MODEL,
        timeout_seconds=settings.AI_TIMEOUT_SECONDS,
        max_tokens=64,
        structured_output_method=settings.AI_STRUCTURED_OUTPUT_METHOD,
    )
    qwen = QwenProvider(
        api_key=settings.QWEN_API_KEY,
        base_url=settings.QWEN_BASE_URL,
        model=settings.QWEN_MODEL,
        timeout_seconds=settings.AI_TIMEOUT_SECONDS,
        max_tokens=64,
        structured_output_method=settings.AI_STRUCTURED_OUTPUT_METHOD,
    )
    qwen_vl = QwenProvider(
        api_key=settings.QWEN_API_KEY,
        base_url=settings.QWEN_BASE_URL,
        model=settings.QWEN_VL_MODEL,
        timeout_seconds=settings.AI_TIMEOUT_SECONDS,
        max_tokens=64,
        structured_output_method=settings.AI_STRUCTURED_OUTPUT_METHOD,
    )

    completions = [
        await deepseek.chat_json(
            messages=[{"role": "user", "content": instruction}],
            response_model=LiveStructuredProbe,
            temperature=0,
        ),
        await qwen.chat_json(
            messages=[{"role": "user", "content": instruction}],
            response_model=LiveStructuredProbe,
            temperature=0,
        ),
        await qwen_vl.vl_chat(
            image_urls=[SYNTHETIC_WHITE_PIXEL_PNG],
            prompt=instruction,
            response_model=LiveStructuredProbe,
            temperature=0,
        ),
    ]

    assert [completion.parsed.marker for completion in completions] == [
        "LANGCHAIN_OK",
        "LANGCHAIN_OK",
        "LANGCHAIN_OK",
    ]
    assert all(completion.trace_id for completion in completions)
    assert all(completion.latency_ms >= 0 for completion in completions)
