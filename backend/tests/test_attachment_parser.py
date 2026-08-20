from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.config import settings
from app.integrations.ai_provider import AiProviderError
from app.integrations.llm_gateway import LlmTask
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


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + width.to_bytes(4, "big") + height.to_bytes(4, "big")


def test_qwen_attachment_schema_normalizes_common_shape_drift() -> None:
    parsed = attachment_parser.AttachmentParseJson.model_validate(
        {
            "file_type": "pdf",
            "summary": {"value": "repair form"},
            "key_points": "SN found",
            "extracted_fields": [],
            "extracted_items": {"sn": "SN001"},
            "raw_text": ["page 1"],
            "warnings": {"code": "TRUNCATED"},
            "truncated": "true",
        }
    )

    assert '"value": "repair form"' in parsed.summary
    assert parsed.key_points == ["SN found"]
    assert parsed.extracted_fields == {}
    assert parsed.extracted_items == [{"sn": "SN001"}]
    assert '"page 1"' in parsed.raw_text
    assert parsed.warnings == ['{"code": "TRUNCATED"}']
    assert parsed.truncated is True


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

    async def fake_invoke_structured(*, task, messages, response_model, temperature):
        del temperature
        seen["task"] = task
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

    monkeypatch.setattr(attachment_parser, "invoke_structured", fake_invoke_structured)
    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)

    attachment = _attachment(file_name=file_name, content_type=content_type, size=len(content))
    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert seen["task"] == LlmTask.ATTACHMENT_TEXT_PARSE
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

    async def fake_invoke_structured(*, task, messages, response_model, temperature):
        del temperature
        seen["task"] = task
        seen["urls"] = [item["image_url"]["url"] for item in messages[0]["content"] if item["type"] == "image_url"]
        return SimpleNamespace(parsed=response_model(file_type="image", summary="visual", raw_text="visual SN001"))

    async def fake_presigned(_session, *, oss_object_id: int, expires_seconds: int) -> str:
        assert oss_object_id == 12
        assert expires_seconds == 1800
        return "https://oss.example.com/signed-image"

    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        assert oss_object_id == 12
        return _png_header(800, 600)

    monkeypatch.setattr(attachment_parser, "invoke_structured", fake_invoke_structured)
    monkeypatch.setattr(attachment_parser, "generate_presigned_url_for_object", fake_presigned)
    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)

    attachment = _attachment(file_name="fault.jpg", content_type="image/jpeg", size=1024)
    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert seen["task"] == LlmTask.ATTACHMENT_VISUAL_PARSE
    assert seen["urls"] == ["https://oss.example.com/signed-image"]
    assert attachment.parse_status == "parsed"
    assert result is not None
    assert result["file_type"] == "image"
    assert result["raw_text"] == "visual SN001"


@pytest.mark.anyio
async def test_pdf_textless_attachment_renders_pages_for_qwen_visual(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")
    monkeypatch.setattr(settings, "PDF_MAX_PARSE_PAGES", 15)
    seen: dict[str, object] = {}

    async def fake_invoke_structured(*, task, messages, response_model, temperature):
        del task, temperature
        seen["urls"] = [item["image_url"]["url"] for item in messages[0]["content"] if item["type"] == "image_url"]
        return SimpleNamespace(parsed=response_model(file_type="pdf", summary="pdf visual", raw_text="PDF SN001"))

    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        assert oss_object_id == 12
        return b"%PDF"

    monkeypatch.setattr(attachment_parser, "invoke_structured", fake_invoke_structured)
    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)
    monkeypatch.setattr(attachment_parser, "_extract_pdf_text", lambda _content, *, max_pages: ("", 18))
    monkeypatch.setattr(attachment_parser, "render_pdf_pages", lambda _content, *, max_pages: ([b"png-page"], 18))

    attachment = _attachment(file_name="scan.pdf", content_type="application/pdf", size=1024)
    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert seen["urls"] == ["data:image/png;base64,cG5nLXBhZ2U="]
    assert attachment.parse_status == "parsed"
    assert result is not None
    assert result["file_type"] == "pdf"
    assert result["truncated"] is True
    assert "PDF_TRUNCATED_TO_15_PAGES" in result["warnings"]


@pytest.mark.anyio
async def test_small_inline_image_is_archived_but_skips_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "INLINE_IMAGE_MIN_PARSE_WIDTH", 256)
    monkeypatch.setattr(settings, "INLINE_IMAGE_MIN_PARSE_HEIGHT", 128)

    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        assert oss_object_id == 12
        return _png_header(88, 18)

    async def fail_presigned(*args, **kwargs):
        del args, kwargs
        raise AssertionError("decorative inline image must not call Qwen")

    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)
    monkeypatch.setattr(attachment_parser, "generate_presigned_url_for_object", fail_presigned)
    attachment = _attachment(file_name="signature.png", content_type="image/png", size=4484)
    attachment.is_inline = True

    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert attachment.oss_object_id == 12
    assert attachment.parse_status == "skipped_decorative"
    assert attachment.parse_error is None
    assert result is not None
    assert result["warnings"] == ["INLINE_DECORATIVE_SKIPPED"]
    assert result["extracted_fields"] == {"image_width": 88, "image_height": 18}


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
async def test_qwen_failure_uses_local_text_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")

    async def failing_invoke_structured(**kwargs):
        del kwargs
        raise AiProviderError("QWEN_TEMPORARILY_UNAVAILABLE")

    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        assert oss_object_id == 12
        return b"SN001 needs repair"

    monkeypatch.setattr(attachment_parser, "invoke_structured", failing_invoke_structured)
    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)
    attachment = _attachment(file_name="fault.txt", content_type="text/plain", size=18)

    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert attachment.parse_status == "parsed"
    assert attachment.parse_error is None
    assert attachment.extracted_text == "SN001 needs repair"
    assert result is not None
    assert result["raw_text"] == "SN001 needs repair"
    assert "QWEN_TEMPORARILY_UNAVAILABLE" in result["warnings"]
    assert "QWEN_FAILED_LOCAL_TEXT_FALLBACK" in result["warnings"]


@pytest.mark.anyio
async def test_text_prc_is_supported_and_uses_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        assert oss_object_id == 12
        return b"TYPE: POW\r\nSerial No.: P80012205200178\r\nPASS"

    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)
    monkeypatch.setattr(attachment_parser, "_qwen_configured", lambda **_kwargs: False)
    attachment = _attachment(file_name="self-check.prc", content_type="application/octet-stream", size=50)

    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert attachment_parser.attachment_type(attachment) == "prc"
    assert attachment.parse_status == "parsed"
    assert attachment.parse_error is None
    assert result is not None
    assert "P80012205200178" in (attachment.extracted_text or "")


@pytest.mark.anyio
async def test_large_txt_skips_attachment_qwen_and_keeps_local_result(monkeypatch: pytest.MonkeyPatch) -> None:
    content = ("SN=P80012205200178\n" * 200).encode()

    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        assert oss_object_id == 12
        return content

    async def fail_if_called(**_kwargs):
        raise AssertionError("large TXT must not call the LLM gateway")

    monkeypatch.setattr(settings, "ATTACHMENT_TEXT_MAX_CHARS", 1000)
    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)
    monkeypatch.setattr(attachment_parser, "invoke_structured", fail_if_called)
    attachment = _attachment(file_name="large.txt", content_type="text/plain", size=len(content))

    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert attachment.parse_status == "parsed"
    assert attachment.parse_error is None
    assert result is not None
    assert result["truncated"] is True
    assert "QWEN_SKIPPED_LARGE_TEXT_LOCAL_FALLBACK" in result["warnings"]


@pytest.mark.anyio
async def test_named_archive_is_skipped_without_download_or_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_download(*args, **kwargs):
        del args, kwargs
        raise AssertionError("recognized archive must not be downloaded for content parsing")

    async def fail_gateway(**kwargs):
        del kwargs
        raise AssertionError("archive must not be sent to Qwen")

    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fail_download)
    monkeypatch.setattr(attachment_parser, "invoke_structured", fail_gateway)
    attachment = _attachment(file_name="self-check.7z", content_type="application/octet-stream", size=128)

    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert attachment.parse_status == "skipped"
    assert attachment.parse_error is None
    assert attachment.extracted_text is None
    assert attachment.content_type == "application/x-7z-compressed"
    assert result is not None
    assert result["attachment_role"] == "engineering_reference"
    assert result["ai_parse_required"] is False
    assert result["blocks_ticket_flow"] is False
    assert result["security_status"] == "unscanned_archive"


@pytest.mark.anyio
async def test_extensionless_legacy_zip_is_detected_from_magic_and_then_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_download(_session, *, oss_object_id: int) -> bytes:
        nonlocal calls
        calls += 1
        assert oss_object_id == 12
        return b"PK\x03\x04data"

    monkeypatch.setattr(attachment_parser, "download_oss_object_bytes", fake_download)
    attachment = _attachment(file_name="20260629_934", content_type="application/octet-stream", size=128)

    result = await attachment_parser.parse_attachment(SimpleNamespace(), attachment)

    assert calls == 1
    assert attachment.parse_status == "skipped"
    assert attachment.parse_error is None
    assert attachment.content_type == "application/zip"
    assert result is not None
    assert result["detected_format"] == "zip"
