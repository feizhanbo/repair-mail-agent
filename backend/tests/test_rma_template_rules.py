from __future__ import annotations

from datetime import date
from email import policy
from email.parser import BytesParser

import pytest

from app.config import settings
from app.models import RepairTicket, ReplyRecord
from app.services import replies
from app.seed import REPLY_TEMPLATES
from app.services.rma_pdf import TEMPLATE_VERSION, normalize_rma_template_version
from app.services.rma_test_preflight import build_rma_test_preflight


def _ticket(**overrides) -> RepairTicket:
    values = {
        "id": 1,
        "ticket_no": "RMATEST0001",
        "language_code": "en-US",
        "customer_name": "Overseas Test Customer",
        "contact_email": "customer@example.com",
        "request_date": date(2026, 7, 1),
        "sn_validation_snapshot": {
            "checks": [{"warranty_start_date": "2026-01-01", "warranty_end_date": "2026-12-31"}]
        },
    }
    values.update(overrides)
    return RepairTicket(**values)


def test_rma_pdf_template_has_one_canonical_version_with_legacy_read_compatibility() -> None:
    assert TEMPLATE_VERSION == "rma_authorization_auto_v3_1"
    assert normalize_rma_template_version("rma_authorization_v1") == TEMPLATE_VERSION
    assert normalize_rma_template_version("rma_authorization_zh_v1") == TEMPLATE_VERSION
    assert normalize_rma_template_version(TEMPLATE_VERSION) == TEMPLATE_VERSION


def test_domestic_and_overseas_replies_use_separate_body_template_versions() -> None:
    zh_type, zh_version = replies._rma_reply_template_type(_ticket(language_code="zh-CN"))
    en_type, en_version = replies._rma_reply_template_type(_ticket())

    assert zh_version == "rma_reply_zh_v1"
    assert en_version == "overseas_in_warranty_v1"
    assert zh_type == "rma_authorization_domestic"
    assert en_type == "rma_authorization_overseas_in_warranty"
    templates = {item["template_type"]: item for item in REPLY_TEMPLATES}
    assert "RMA维修授权表见附件" in templates[zh_type]["body_template"]
    assert "RMA authorization form is attached" in templates[en_type]["body_template"]
    assert TEMPLATE_VERSION not in {zh_version, en_version}


def test_overseas_out_of_warranty_and_special_rules() -> None:
    template_type, version = replies._rma_reply_template_type(
        _ticket(request_date=date(2027, 1, 1))
    )
    assert version == "overseas_out_warranty_v1"
    assert template_type == "rma_authorization_overseas_out_of_warranty"

    st_type, st_version = replies._rma_reply_template_type(
        _ticket(customer_name="STMicroelectronics Pte Ltd", request_date=date(2026, 12, 31))
    )
    assert st_version == "overseas_st_pickup_v1"
    assert st_type == "rma_authorization_overseas_st_pickup"

    with pytest.raises(replies.RmaReplyRuleError) as amkor:
        replies._rma_reply_template_type(_ticket(contact_email="person@amkor.com"))
    assert amkor.value.task_type == "rma_amkor_manual"

    with pytest.raises(replies.RmaReplyRuleError) as daniel:
        replies._rma_reply_template_type(_ticket(contact_email="daniel@leitik.com", request_date=date(2027, 1, 1)))
    assert daniel.value.task_type == "rma_price_required"


def test_rma_mime_contains_one_pdf_and_never_inherits_cc(monkeypatch) -> None:
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest1@accotest.com")
    reply = ReplyRecord(
        id=9,
        ticket_id=1,
        reply_type="rma_authorization",
        to_addresses="rmatest2@accotest.com",
        cc_addresses=None,
        subject="[TEST ONLY] RMA attachment validation",
        final_body="TEST ONLY synthetic message",
    )
    raw = replies._build_reply_message(
        reply,
        "<test-only@accotest.com>",
        attachment_content=b"%PDF-1.7\nTEST ONLY",
        attachment_filename="RMATEST.pdf",
    ).as_bytes(policy=policy.SMTP)
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    attachments = list(parsed.iter_attachments())

    assert parsed["From"] == "rmatest1@accotest.com"
    assert parsed["To"] == "rmatest2@accotest.com"
    assert parsed.get("Cc") is None
    assert parsed.get("Bcc") is None
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_filename() == "RMATEST.pdf"


def test_offline_preflight_fails_for_wrong_smtp_login_without_network(monkeypatch) -> None:
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest2@accotest.com")
    monkeypatch.setattr(settings, "SMTP_RECIPIENT_WHITELIST", ["rmatest2@accotest.com"])

    preflight = build_rma_test_preflight(timestamp="20260716180000")

    assert preflight.result["status"] == "failed"
    assert preflight.result["reasons"] == ["SMTP_LOGIN_MUST_BE_RMATEST1", "MIME_FROM_MISMATCH"]
    assert preflight.result["network_connected"] is False
    assert preflight.result["send_count"] == 0
    assert preflight.result["attachment_count"] == 1
    assert preflight.result["attachment_sha256"] == preflight.result["pdf_sha256"]


def test_offline_preflight_passes_only_for_exact_sender_and_single_recipient_whitelist(monkeypatch) -> None:
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "SMTP_RECIPIENT_WHITELIST", ["rmatest2@accotest.com"])

    preflight = build_rma_test_preflight(timestamp="20260716180001")

    assert preflight.result["status"] == "passed"
    assert preflight.result["reasons"] == []
    assert preflight.result["from"] == "rmatest1@accotest.com"
    assert preflight.result["to"] == "rmatest2@accotest.com"
    assert preflight.result["cc"] == []
    assert preflight.result["bcc"] == []
    assert preflight.result["rma_template_version"] == "rma_authorization_auto_v3_1"
    assert preflight.result["pdf_page_count"] == 3
    assert preflight.result["watermarked_page_count"] == 3
