from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.integrations.llm_gateway import LlmTask
from app.models import EmailAttachment
from app.services import ai, attachment_parser


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_text_ai_configuration_accepts_configured_fallback_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")

    assert ai.text_ai_configured() is True
    assert ai.ai_configured() is True
    assert ai.multimodal_ai_configured() is True


@pytest.mark.anyio
async def test_multimodal_attachment_uses_qwen_vl_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_MODEL", "qwen-plus")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")

    async def fake_invoke_structured(*, task, messages, response_model, temperature):
        del messages, temperature
        seen["task"] = task
        return SimpleNamespace(
            parsed=response_model(
                file_type="image",
                extracted_fields={"sn": "SN001"},
                extracted_items=[{"sn": "SN001"}],
                raw_text="SN001",
            )
        )

    async def fake_presigned_url(_session, *, oss_object_id: int, expires_seconds: int) -> str:
        assert oss_object_id == 12
        assert expires_seconds == 1800
        return "https://oss.example.com/signed-image"

    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        assert oss_object_id == 12
        return b"image-bytes"

    monkeypatch.setattr(attachment_parser, "invoke_structured", fake_invoke_structured)
    monkeypatch.setattr(attachment_parser, "generate_presigned_url_for_object", fake_presigned_url)
    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)
    attachment = EmailAttachment(
        email_id=1,
        file_name="fault.png",
        content_type="image/png",
        parse_status="pending",
        oss_object_id=12,
    )

    result = await ai.parse_attachment_multimodal(SimpleNamespace(), attachment)

    assert seen["task"] == LlmTask.ATTACHMENT_VISUAL_PARSE
    assert result is not None
    assert result["extracted_fields"] == {"sn": "SN001"}
