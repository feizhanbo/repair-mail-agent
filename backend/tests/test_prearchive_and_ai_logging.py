from __future__ import annotations

import base64
import json

import pytest
from fastapi import HTTPException

from app.config import settings
from app.models import AiCallLog, Email, EmailAttachment
from app.schemas.business import EmailIngestRequest
from app.services import ai, email_preview
from app.services.ai import read_ai_log_detail, sanitize_ai_detail
from app.services.attachment_precheck import (
    detect_archive_format,
    engineering_reference_metadata,
    filter_decorative_attachments,
)
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


@pytest.mark.parametrize(
    ("file_name", "content_type", "content", "expected_format"),
    [
        ("files.zip", "application/zip", b"PK\x03\x04data", "zip"),
        ("files.rar", "application/vnd.rar", b"Rar!\x1a\x07\x00data", "rar"),
        ("files.7z", "application/x-7z-compressed", b"7z\xbc\xaf\x27\x1cdata", "7z"),
        ("files.tar", "application/x-tar", b"\x00" * 257 + b"ustar" + b"\x00", "tar"),
        ("files.tar.gz", "application/gzip", b"\x1f\x8bdata", "tar_gz"),
        ("files.tgz", "application/gzip", b"\x1f\x8bdata", "tar_gz"),
        ("files.gz", "application/gzip", b"\x1f\x8bdata", "gzip"),
        ("20260629_934", "application/octet-stream", b"PK\x03\x04data", "zip"),
    ],
)
def test_archive_detector_uses_magic_mime_and_filename(
    file_name: str,
    content_type: str,
    content: bytes,
    expected_format: str,
) -> None:
    detected, warnings = detect_archive_format(
        file_name=file_name,
        content_type=content_type,
        content=content,
    )

    assert detected == expected_format
    assert warnings == []


def test_archive_detector_prefers_magic_and_records_declaration_mismatch() -> None:
    detected, warnings = detect_archive_format(
        file_name="self-check.rar",
        content_type="application/vnd.rar",
        content=b"PK\x03\x04data",
    )

    assert detected == "zip"
    assert warnings == ["TYPE_DECLARATION_MISMATCH"]


def test_archive_detector_records_mime_mismatch_even_when_extension_matches_magic() -> None:
    detected, warnings = detect_archive_format(
        file_name="self-check.zip",
        content_type="application/vnd.rar",
        content=b"PK\x03\x04data",
    )

    assert detected == "zip"
    assert warnings == ["TYPE_DECLARATION_MISMATCH"]


def test_non_zip_magic_overrides_false_office_declaration() -> None:
    detected, warnings = detect_archive_format(
        file_name="self-check.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content=b"Rar!\x1a\x07\x00data",
    )

    assert detected == "rar"
    assert warnings == ["TYPE_DECLARATION_MISMATCH"]


@pytest.mark.parametrize(
    ("file_name", "content_type"),
    [
        ("repair.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("repair.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ],
)
def test_office_open_xml_is_not_classified_as_engineering_archive(file_name: str, content_type: str) -> None:
    detected, warnings = detect_archive_format(
        file_name=file_name,
        content_type=content_type,
        content=b"PK\x03\x04data",
    )

    assert detected is None
    assert warnings == []


def test_archive_is_marked_as_non_blocking_engineering_reference_before_archival() -> None:
    payload = EmailIngestRequest(
        mailbox_account="test",
        from_address="sender@example.com",
        attachments=[{"file_name": "20260629_934", "content_type": "application/octet-stream"}],
    )
    blobs = [{"file_name": "20260629_934", "content_type": "application/octet-stream", "content": b"PK\x03\x04data"}]

    kept, result = filter_decorative_attachments(payload, blobs)

    assert result.kept_count == 1
    assert kept[0]["content_type"] == "application/zip"
    attachment = payload.attachments[0]
    assert attachment["content_type"] == "application/zip"
    assert attachment["parse_status"] == "skipped"
    assert attachment["parse_error"] is None
    assert attachment["extracted_text"] is None
    metadata = attachment["extracted_json"]
    assert metadata["file_type"] == "archive"
    assert metadata["detected_format"] == "zip"
    assert metadata["attachment_role"] == "engineering_reference"
    assert metadata["business_required"] is False
    assert metadata["ai_parse_required"] is False
    assert metadata["blocks_ticket_flow"] is False
    assert metadata["security_status"] == "unscanned_archive"
    assert metadata["parse_skip_reason"] == "ENGINEERING_REFERENCE_NOT_REQUIRED"
    assert metadata["detection_warnings"] == []
    assert isinstance(metadata["classified_at"], str)


def test_engineering_archive_ai_input_contains_only_classification_metadata() -> None:
    email = Email(
        id=1,
        mailbox_account="test",
        message_id="<archive@test>",
        from_address="sender@example.com",
        subject="Repair",
        text_body="Device cannot start.",
    )
    attachment = EmailAttachment(
        id=7,
        email_id=1,
        file_name="self-check.zip",
        content_type="application/zip",
        parse_status="skipped",
        extracted_text="SN-MUST-NOT-REACH-AI",
        parse_error="MUST_NOT_REACH_AI",
        extracted_json={
            **engineering_reference_metadata("zip"),
            "extracted_fields": {"problem_description": "MUST_NOT_BE_USED"},
            "extracted_items": [{"sn": "MUST_NOT_BE_USED"}],
        },
    )

    payload = ai._email_input(email, [attachment], "field_extract")
    item = payload["attachments"][0]

    assert item["file_name"] == "self-check.zip"
    assert item["classification"]["detected_format"] == "zip"
    assert "extracted_text" not in item
    assert "parse_error" not in item
    assert "extracted_fields" not in item["classification"]
    assert "extracted_items" not in item["classification"]
    assert ai._structured_attachment_business_data([attachment]) == ({}, [], [])


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
        file_name="firmware.dat",
        content_type="application/octet-stream",
        parse_status="unsupported",
        oss_object_id=9,
    )
    preview = await build_attachment_preview(PreviewSession(attachment), 5)

    assert preview["mode"] == "download_only"
    assert preview["warnings"][0]["code"] == "ATTACHMENT_PREVIEW_UNSUPPORTED"


@pytest.mark.anyio
async def test_engineering_archive_preview_reports_unscanned_instead_of_unsupported() -> None:
    attachment = EmailAttachment(
        id=7,
        email_id=1,
        file_name="self-check.zip",
        content_type="application/zip",
        parse_status="skipped",
        oss_object_id=11,
        extracted_json={
            "file_type": "archive",
            "attachment_role": "engineering_reference",
            "security_status": "unscanned_archive",
        },
    )

    preview = await build_attachment_preview(PreviewSession(attachment), 7)

    assert preview["mode"] == "download_only"
    assert preview["warnings"][0]["code"] == "ARCHIVE_CONTENT_NOT_SCANNED"
    assert "未经内容扫描" in preview["warnings"][0]["message"]


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
