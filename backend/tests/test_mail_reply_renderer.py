from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest

from app.models import ReplyRecord
from app.services import replies
from app.services.mail_reply_renderer import ReplyRenderError, render_reply_history_from_eml


def _rich_parent_eml(*, duplicate_cid: bool = False, missing_cid: bool = False) -> bytes:
    message = EmailMessage()
    message["From"] = "Customer <customer@example.com>"
    message["To"] = "rmatest1@accotest.com"
    message["Subject"] = "Board repair"
    message["Message-ID"] = "<parent@example.com>"
    message.set_content("Latest customer text\nEarlier reply\nOriginal request")
    cid = "missing-image" if missing_cid else "image001"
    message.add_alternative(
        """
        <html><head>
          <style>.safe { color: blue; background-image:url(cid:image001) }</style>
          <script>alert(1)</script>
        </head><body onload="alert(2)">
          <table><tr><td>SN001</td><td>Calibration fail</td></tr></table>
          <img src="cid:%s" alt="cid:not-a-rendered-reference">
          <img src="https://example.com/logo.png">
          <img src="data:image/png;base64,QUJD">
          <a href="javascript:alert(3)" onclick="alert(4)">unsafe</a>
          <blockquote>Earlier reply<br>Original request</blockquote>
        </body></html>
        """ % cid,
        subtype="html",
    )
    html_part = message.get_body(preferencelist=("html",))
    assert html_part is not None
    html_part.add_related(
        b"inline-image",
        maintype="image",
        subtype="png",
        cid="<image001>",
        filename="looks-like-an-attachment.png",
        disposition="attachment",
    )
    if duplicate_cid:
        html_part.add_related(
            b"different-image",
            maintype="image",
            subtype="jpeg",
            cid="<image001>",
            disposition="inline",
        )
    message.add_attachment(
        b"old-spreadsheet",
        maintype="application",
        subtype="vnd.ms-excel",
        filename="old.xls",
    )
    message.add_attachment(
        b"old-pdf",
        maintype="application",
        subtype="pdf",
        filename="old.pdf",
    )
    return message.as_bytes()


def test_renderer_preserves_rich_body_and_only_cid_dependencies() -> None:
    history = render_reply_history_from_eml(
        _rich_parent_eml(),
        parent_email_id=782,
        language="zh-CN",
    )

    assert "<table>" in history.html
    assert "SN001" in history.html
    assert "blockquote" in history.html
    assert "https://example.com/logo.png" in history.html
    assert "data:image/png;base64,QUJD" in history.html
    assert "script" not in history.html.lower()
    assert "onload" not in history.html.lower()
    assert "onclick" not in history.html.lower()
    assert "javascript:" not in history.html.lower()
    assert "cid:not-a-rendered-reference" in history.html
    assert "cid:image001" not in history.html.lower()
    assert "cid:history-782-" in history.html.lower()
    assert len(history.resources) == 1
    assert history.resources[0].content == b"inline-image"
    assert history.resources[0].content_id.startswith("history-782-")
    assert "old.xls" not in history.plain
    assert "old.pdf" not in history.plain
    assert len(history.snapshot_hash) == 64


def test_renderer_ignores_cid_text_outside_rendering_urls() -> None:
    history = render_reply_history_from_eml(
        _rich_parent_eml(),
        parent_email_id=1,
        language="en-US",
    )
    assert len(history.resources) == 1
    assert "cid:not-a-rendered-reference" in history.html


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing", "REPLY_PARENT_CID_MISSING"),
        ("duplicate", "REPLY_PARENT_CID_CONFLICT"),
    ],
)
def test_renderer_blocks_missing_or_conflicting_cid(case: str, expected: str) -> None:
    raw = _rich_parent_eml(
        missing_cid=case == "missing",
        duplicate_cid=case == "duplicate",
    )
    with pytest.raises(ReplyRenderError) as caught:
        render_reply_history_from_eml(raw, parent_email_id=2, language="zh-CN")
    assert caught.value.code == expected


def test_renderer_recovers_missing_known_accotest_logo_from_customer_quote() -> None:
    message = EmailMessage()
    message["From"] = "rmatest2@accotest.com"
    message["To"] = "rmatest1@accotest.com"
    message["Subject"] = "Re: repair"
    message["Message-ID"] = "<supplement@example.com>"
    message.set_content("Supplement information with quoted system signature")
    message.add_alternative(
        '<div>Supplement information<img src="cid:accotest_logo"></div>',
        subtype="html",
    )

    history = render_reply_history_from_eml(
        message.as_bytes(), parent_email_id=22, language="zh-CN"
    )

    assert len(history.resources) == 1
    assert history.resources[0].original_content_id == "accotest_logo"
    assert "cid:history-22-" in history.html


def test_renderer_plain_text_fallback_is_safe_html() -> None:
    message = EmailMessage()
    message["From"] = "customer@example.com"
    message["To"] = "rmatest1@accotest.com"
    message["Subject"] = "Plain repair"
    message["Message-ID"] = "<plain@example.com>"
    message.set_content("SN001 <script>alert(1)</script>\nCalibration fail")

    history = render_reply_history_from_eml(
        message.as_bytes(),
        parent_email_id=3,
        language="en-US",
    )

    assert "SN001 &lt;script&gt;alert(1)&lt;/script&gt;" in history.html
    assert not history.resources


def test_reply_mime_contains_related_resources_and_only_new_business_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(replies.settings, "SMTP_USER", "rmatest1@accotest.com")
    history = render_reply_history_from_eml(
        _rich_parent_eml(),
        parent_email_id=782,
        language="zh-CN",
    )
    reply = ReplyRecord(
        id=8,
        ticket_id=2,
        reply_type="rma_authorization",
        to_addresses="rmatest2@accotest.com",
        subject="RMA2026081201客户",
        draft_body="本次RMA回复\n\n" + history.plain,
        final_body="本次RMA回复\n\n" + history.plain,
        draft_html_body="<div>本次RMA回复</div>" + history.html,
        final_html_body="<div>本次RMA回复</div>" + history.html,
        in_reply_to="<parent@example.com>",
        references_header="<root@example.com> <parent@example.com>",
    )

    result = replies._build_reply_message(
        reply,
        "<reply@example.com>",
        related_resources=history.resources,
        attachment_content=b"%PDF-current",
        attachment_filename="RMA2026081201客户.pdf",
    )
    reparsed = BytesParser(policy=policy.default).parsebytes(result.as_bytes())
    cids = [str(part.get("Content-ID") or "").strip("<>") for part in reparsed.walk()]
    business_attachments = [part.get_filename() for part in reparsed.iter_attachments()]

    assert history.resources[0].content_id in cids
    assert business_attachments == ["RMA2026081201客户.pdf"]
    assert reparsed["In-Reply-To"] == "<parent@example.com>"
    assert reparsed["References"] == "<root@example.com> <parent@example.com>"


def test_reference_snapshot_is_deterministic() -> None:
    raw = _rich_parent_eml()
    first = render_reply_history_from_eml(raw, parent_email_id=782, language="zh-CN")
    second = render_reply_history_from_eml(raw, parent_email_id=782, language="zh-CN")

    assert first.snapshot_hash == second.snapshot_hash
    assert first.html == second.html
    assert [item.content_id for item in first.resources] == [item.content_id for item in second.resources]
