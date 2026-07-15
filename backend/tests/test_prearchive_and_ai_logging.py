from __future__ import annotations

import base64

from app.schemas.business import EmailIngestRequest
from app.services.ai import sanitize_ai_detail
from app.services.attachment_precheck import filter_decorative_attachments
from app.services.email_preview import sanitize_email_html


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
