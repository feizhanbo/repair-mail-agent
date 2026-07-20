from __future__ import annotations

import base64
import json

import pytest
from fastapi import HTTPException

from app.config import settings
from app.models import AiCallLog, EmailAttachment
from app.schemas.business import EmailIngestRequest
from app.services import email_preview
from app.services.ai import read_ai_log_detail, sanitize_ai_detail
from app.services.attachment_precheck import filter_decorative_attachments
from app.services.email_preview import build_attachment_preview, sanitize_email_html
from app.services.common import sha256_text


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class PreviewSession:
    def __init__(self, value) -> None:
        self.value = value

    async def get(self, _model, _object_id):
        return self.value


def _png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + width.to_bytes(4, "big") + height.to_bytes(4, "big")


def test_decorative_inline_image_is_removed_before_archival() -> None:
    payload = EmailIngestRequest(
        mailbox_account="test",
        from_address="sender@example.com",
        attachments=[
            {"file_name": "logo.png", "content_type": "image/png", "is_inline": True, "content_id": "logo"},
            {"file_name": "fault.png", "content_type": "image/png", "is_inline": True, "content_id": "fault"},
        ],
    )
    blobs = [
        {"file_name": "logo.png", "content_type": "image/png", "content": _png(64, 32), "is_inline": True},
        {"file_name": "fault.png", "content_type": "image/png", "content": _png(1024, 768), "is_inline": True},
    ]

    kept, result = filter_decorative_attachments(payload, blobs)

    assert [item["file_name"] for item in kept] == ["fault.png"]
    assert [item["file_name"] for item in payload.attachments] == ["fault.png"]
    assert result.skipped_decorative_count == 1


def test_ai_detail_preserves_prompts_but_redacts_transport_secrets() -> None:
    data_url = "data:image/png;base64," + base64.b64encode(b"binary").decode()
    result = sanitize_ai_detail(
        {
            "api_key": "secret",
            "messages": [{"role": "user", "content": "完整提示词"}],
            "image_url": "https://bucket.example.com/object.png?Signature=secret&Expires=1",
            "data": data_url,
            "total_tokens": 42,
        }
    )

    assert result["api_key"] == "[REDACTED]"
    assert result["messages"][0]["content"] == "完整提示词"
    assert result["image_url"].endswith("#SIGNED_QUERY_REDACTED")
    assert result["data"]["binary_ref"] is True
    assert result["total_tokens"] == 42


def test_email_html_preview_blocks_active_and_remote_content() -> None:
    html = '<script>alert(1)</script><img src="https://tracker.test/pixel"><img src="cid:fault"><a href="javascript:alert(1)" onclick="x()">link</a>'
    sanitized = sanitize_email_html(html, cid_urls={"fault": "https://oss.test/fault.png?signature=short"})

    assert "script" not in sanitized
    assert "tracker.test" not in sanitized
    assert "onclick" not in sanitized
    assert "javascript:" not in sanitized
    assert "oss.test/fault.png" in sanitized


@pytest.mark.anyio
async def test_ai_detail_distinguishes_full_metadata_expired_and_corrupt(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_LOG_DIR", str(tmp_path))
    record = {
        "input_payload": {"subject": "test"},
        "request_payload": {"model": "mock"},
        "response_payload": {"answer": "ok"},
        "parsed_result": {"intent": "new_repair"},
        "token_usage": {"input": 3, "output": 2, "total": 5},
    }
    raw = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    path = tmp_path / "detail.jsonl"
    path.write_text(raw + "\n", encoding="utf-8")
    base = dict(
        trace_id="trace",
        call_type="email_parse",
        model_name="mock",
        prompt_version="v1",
        status="success",
    )

    full = AiCallLog(**base, log_file_path=str(path), log_line_no=1, log_record_hash=sha256_text(raw))
    assert (await read_ai_log_detail(full))["availability"] == "full"

    metadata = AiCallLog(**base, log_file_path="", log_line_no=None)
    assert (await read_ai_log_detail(metadata))["availability"] == "metadata_only"

    expired = AiCallLog(**base, log_file_path=str(tmp_path / "missing.jsonl"), log_line_no=1)
    assert (await read_ai_log_detail(expired))["availability"] == "expired"

    corrupt = AiCallLog(**base, log_file_path=str(path), log_line_no=1, log_record_hash="0" * 64)
    assert (await read_ai_log_detail(corrupt))["availability"] == "corrupt"


@pytest.mark.anyio
async def test_unsupported_attachment_is_download_only_without_binary_decode() -> None:
    attachment = EmailAttachment(
        id=5,
        email_id=1,
        file_name="firmware.prc",
        content_type="application/octet-stream",
        parse_status="unsupported",
        oss_object_id=9,
    )
    preview = await build_attachment_preview(PreviewSession(attachment), 5)

    assert preview["mode"] == "download_only"
    assert preview["warnings"][0]["code"] == "ATTACHMENT_PREVIEW_UNSUPPORTED"


@pytest.mark.anyio
async def test_invalid_pdf_returns_safe_stage_and_retry_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    attachment = EmailAttachment(
        id=6,
        email_id=1,
        file_name="broken.pdf",
        content_type="application/pdf",
        parse_status="parsed",
        oss_object_id=10,
    )

    async def fake_download(*_args, **_kwargs):
        return b"not-a-pdf"

    monkeypatch.setattr(email_preview, "_download_bytes", fake_download)
    with pytest.raises(HTTPException) as exc_info:
        await build_attachment_preview(PreviewSession(attachment), 6)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "ATTACHMENT_PDF_INVALID"
    assert exc_info.value.detail["data"]["stage"] == "attachment_pdf_render"
    assert exc_info.value.detail["data"]["retryable"] is False
