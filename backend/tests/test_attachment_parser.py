from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.config import settings
from app.integrations.ai_provider import AiProviderError
from app.models import EmailAttachment
from app.services import attachment_parser


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _docx_bytes(text: str) -> bytes:
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _xlsx_bytes(text: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "repairs"
    sheet.append(["sn", "description"])
    sheet.append([text, "fault"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _attachment(*, file_name: str, content_type: str, size: int, object_id: int = 12) -> EmailAttachment:
    return EmailAttachment(
        email_id=1,
        file_name=file_name,
        content_type=content_type,
        file_size=size,
        parse_status="pending",
        oss_object_id=object_id,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("file_name", "content_type", "content", "expected_type", "expected_prompt_fragment"),
    [
        ("fault.txt", "text/plain", b"SN-TXT repair request", "txt", "SN-TXT"),
        ("fault.csv", "text/csv", b"sn,desc\nSN-CSV,fault\n", "csv", "SN-CSV"),
        ("fault.html", "text/html", b"<html><body>HTML SN</body></html>", "html", "HTML SN"),
        ("fault.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", _docx_bytes("DOCX SN"), "docx", "DOCX SN"),
        ("fault.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _xlsx_bytes("XLSX SN"), "xlsx", "XLSX SN"),
    ],
)
async def test_supported_text_like_attachments_extract_then_use_qwen(
    monkeypatch: pytest.MonkeyPatch,
    file_name: str,
    content_type: str,
    content: bytes,
    expected_type: str,
    expected_prompt_fragment: str,
) -> None:
    seen: dict[str, list[str] | str] = {"prompts": []}
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "QWEN_MODEL", "qwen-plus")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")

    class FakeQwenProvider:
        def __init__(self, *, api_key: str, base_url: str, model: str, timeout_seconds: float):
            del api_key, base_url, timeout_seconds
            seen["model"] = model

        async def chat_json(self, *, messages, response_model, **kwargs):
            del kwargs
            seen["prompts"].append(messages[-1]["content"])
            return SimpleNamespace(
                parsed=response_model(
                    file_type=expected_type,
                    summary="parsed",
                    key_points=["SN"],
                    extracted_fields={"sn": expected_prompt_fragment},
                    raw_text=f"qwen raw {expected_prompt_fragment}",
                )
            )

    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        assert oss_object_id == 12
        return content

    monkeypatch.setattr(attachment_parser, "QwenProvider", FakeQwenProvider)
    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)

    attachment = _attachment(file_name=file_name, content_type=content_type, size=len(content))
    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert seen["model"] == "qwen-vl-plus"
    assert any(expected_prompt_fragment in prompt for prompt in seen["prompts"])
    assert attachment.parse_status == "parsed"
    assert attachment.parse_error is None
    assert attachment.extracted_text == f"qwen raw {expected_prompt_fragment}"
    assert result is not None
    assert result["file_type"] == expected_type
    assert result["extracted_fields"] == {"sn": expected_prompt_fragment}


@pytest.mark.anyio
async def test_image_attachment_uses_presigned_url_and_qwen_visual(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "QWEN_MODEL", "qwen-plus")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")

    class FakeQwenProvider:
        def __init__(self, **kwargs):
            seen["model"] = kwargs["model"]

        async def vl_chat(self, *, image_urls, response_model, **kwargs):
            del kwargs
            seen["urls"] = image_urls
            return SimpleNamespace(parsed=response_model(file_type="image", summary="visual", raw_text="visual SN001"))

    async def fake_presigned(_session, *, oss_object_id: int, expires_seconds: int) -> str:
        assert oss_object_id == 12
        assert expires_seconds == 1800
        return "https://oss.example.com/signed-image"

    monkeypatch.setattr(attachment_parser, "QwenProvider", FakeQwenProvider)
    monkeypatch.setattr(attachment_parser, "generate_presigned_url_for_object", fake_presigned)

    attachment = _attachment(file_name="fault.jpg", content_type="image/jpeg", size=1024)
    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert seen["model"] == "qwen-vl-plus"
    assert seen["urls"] == ["https://oss.example.com/signed-image"]
    assert attachment.parse_status == "parsed"
    assert result is not None
    assert result["file_type"] == "image"
    assert result["raw_text"] == "visual SN001"


@pytest.mark.anyio
async def test_pdf_textless_attachment_uses_visual_url_and_records_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")
    monkeypatch.setattr(settings, "PDF_MAX_PARSE_PAGES", 15)
    seen: dict[str, object] = {}

    class FakeQwenProvider:
        def __init__(self, **kwargs):
            del kwargs

        async def vl_chat(self, *, image_urls, response_model, **kwargs):
            del kwargs
            seen["urls"] = image_urls
            return SimpleNamespace(parsed=response_model(file_type="pdf", summary="pdf visual", raw_text="PDF SN001"))

    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        assert oss_object_id == 12
        return b"%PDF"

    async def fake_presigned(_session, *, oss_object_id: int, expires_seconds: int) -> str:
        del expires_seconds
        assert oss_object_id == 12
        return "https://oss.example.com/signed-pdf"

    monkeypatch.setattr(attachment_parser, "QwenProvider", FakeQwenProvider)
    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)
    monkeypatch.setattr(attachment_parser, "generate_presigned_url_for_object", fake_presigned)
    monkeypatch.setattr(attachment_parser, "_extract_pdf_text", lambda _content, *, max_pages: ("", 18))
    monkeypatch.setattr(attachment_parser, "_first_pdf_pages", lambda _content, *, max_pages: None)

    attachment = _attachment(file_name="scan.pdf", content_type="application/pdf", size=1024)
    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert seen["urls"] == ["https://oss.example.com/signed-pdf"]
    assert attachment.parse_status == "parsed"
    assert result is not None
    assert result["file_type"] == "pdf"
    assert result["truncated"] is True
    assert "PDF_TRUNCATED_TO_15_PAGES" in result["warnings"]


@pytest.mark.anyio
async def test_oversized_attachment_is_uploaded_but_not_auto_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ATTACHMENT_MAX_AUTO_PARSE_BYTES", 10)

    async def fail_download(*args, **kwargs):
        del args, kwargs
        raise AssertionError("oversized attachment should not be downloaded for parsing")

    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fail_download)
    attachment = _attachment(file_name="large.txt", content_type="text/plain", size=11)

    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert attachment.parse_status == "needs_manual_review"
    assert attachment.parse_error == "FILE_TOO_LARGE_FOR_AUTO_PARSE"
    assert result is not None
    assert result["warnings"] == ["FILE_TOO_LARGE_FOR_AUTO_PARSE"]
    assert result["truncated"] is True


@pytest.mark.anyio
async def test_qwen_failure_marks_attachment_manual_without_blocking_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")

    class FailingQwenProvider:
        def __init__(self, **kwargs):
            del kwargs

        async def chat_json(self, **kwargs):
            del kwargs
            raise AiProviderError("QWEN_TEMPORARILY_UNAVAILABLE")

    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        assert oss_object_id == 12
        return b"SN001 needs repair"

    monkeypatch.setattr(attachment_parser, "QwenProvider", FailingQwenProvider)
    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)
    attachment = _attachment(file_name="fault.txt", content_type="text/plain", size=18)

    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert attachment.parse_status == "needs_manual_review"
    assert attachment.parse_error == "QWEN_TEMPORARILY_UNAVAILABLE"
    assert attachment.extracted_text == "SN001 needs repair"
    assert result is not None
    assert result["raw_text"] == "SN001 needs repair"
    assert "QWEN_TEMPORARILY_UNAVAILABLE" in result["warnings"]
