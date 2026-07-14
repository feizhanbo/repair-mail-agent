from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.models import EmailAttachment, OssObject
from app.services import ai


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_text_ai_configuration_ignores_qwen_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")

    assert ai.text_ai_configured() is False
    assert ai.ai_configured() is False
    assert ai.multimodal_ai_configured() is True


@pytest.mark.anyio
async def test_multimodal_attachment_uses_qwen_vl_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_MODEL", "qwen-plus")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")

    class FakeQwenProvider:
        def __init__(self, *, api_key: str, base_url: str, model: str, timeout_seconds: float):
            del api_key, base_url, timeout_seconds
            seen["model"] = model

        async def vl_chat(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                parsed=ai.MultimodalParseResult(
                    extracted_fields={"sn": "SN001"},
                    extracted_items=[{"sn": "SN001"}],
                    raw_text="SN001",
                )
            )

    class FakeSession:
        async def get(self, model, _id: int):
            if model is OssObject:
                obj = OssObject(
                    bucket="repair-mail",
                    endpoint="https://oss.example.com",
                    object_key="attachments/fault.png",
                    source_type="email_attachment",
                    upload_status="success",
                )
                obj.id = _id
                return obj
            return None

    monkeypatch.setattr(ai, "QwenProvider", FakeQwenProvider)
    attachment = EmailAttachment(
        email_id=1,
        file_name="fault.png",
        content_type="image/png",
        parse_status="pending",
        oss_object_id=12,
    )

    result = await ai.parse_attachment_multimodal(FakeSession(), attachment)

    assert seen["model"] == "qwen-vl-plus"
    assert result is not None
    assert result["extracted_fields"] == {"sn": "SN001"}
