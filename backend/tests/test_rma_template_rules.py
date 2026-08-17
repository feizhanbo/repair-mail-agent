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
    assert TEMPLATE_VERSION == "rma_authorization_v3_2_reference"
    assert normalize_rma_template_version("rma_authorization_v1") == TEMPLATE_VERSION
    assert normalize_rma_template_version("rma_authorization_zh_v1") == TEMPLATE_VERSION
    assert normalize_rma_template_version(TEMPLATE_VERSION) == TEMPLATE_VERSION


def test_domestic_and_overseas_replies_use_separate_body_template_versions() -> None:
    zh_type, zh_version = replies._rma_reply_template_type(_ticket(language_code="zh-CN"))
    en_type, en_version = replies._rma_reply_template_type(_ticket())

    assert zh_version == "domestic_in_warranty_v1"
    assert en_version == "overseas_in_warranty_v1"
    assert zh_type == "rma_authorization_domestic_in_warranty"
    assert en_type == "rma_authorization_overseas_in_warranty"
    templates = {item["template_type"]: item for item in REPLY_TEMPLATES}
    assert "RMA表格见附件" in templates[zh_type]["body_template"]
    assert "RMA authorization form is attached" in templates[en_type]["body_template"]
    assert TEMPLATE_VERSION not in {zh_version, en_version}
    assert all(
        "{{ return_address_block }}" in item["body_template"]
        for item in REPLY_TEMPLATES
        if item["template_type"].startswith("rma_authorization")
    )


def test_domestic_rma_templates_and_miya_signature_match_approved_copy() -> None:
    rma = next(
        item for item in REPLY_TEMPLATES
        if item["template_type"] == "rma_authorization_domestic_in_warranty"
    )
    base = next(
        item for item in REPLY_TEMPLATES
        if item["template_type"] == "domestic_company_base" and item["version"] == "v2"
    )
    required_rma_phrases = (
        "Dear {{ contact_person }}",
        "RMA表格见附件",
        "请务必打印出RMA表",
        "{{ city }}质量部",
        "维修工期：10个工作日",
        "{{ return_address_block }}",
    )
    required_signature_phrases = (
        "Miya Fang (方菲)",
        "+86-512-67678157/62982753*801",
        "86-15001161080",
        "miya.fang@accotest.com",
        "江苏省苏州市工业园区新平街388号",
        "The information contained in and accompanying this email may be confidential",
    )

    for phrase in required_rma_phrases:
        assert phrase in rma["body_template"]
        assert phrase in rma["html_body_template"]
    for phrase in required_signature_phrases:
        assert phrase in base["body_template"]
        assert phrase in base["html_body_template"]
    assert 'cid:accotest_logo' in base["html_body_template"]


def test_return_address_blocks_match_confirmed_business_text() -> None:
    beijing = replies._return_address_block(
        language="zh-CN",
        customer_policy={
            "shipping_company": settings.RMA_DEFAULT_BEIJING_COMPANY,
            "shipping_address": settings.RMA_DEFAULT_BEIJING_ADDRESS,
            "shipping_contact": settings.RMA_DEFAULT_BEIJING_CONTACT,
            "shipping_phone": settings.RMA_DEFAULT_BEIJING_PHONE,
            "shipping_postal_code": settings.RMA_DEFAULT_BEIJING_POSTAL_CODE,
        },
    )
    tianjin = replies._return_address_block(
        language="zh-CN",
        customer_policy={
            "shipping_company": settings.RMA_DEFAULT_TIANJIN_COMPANY,
            "shipping_address": settings.RMA_DEFAULT_TIANJIN_ADDRESS,
            "shipping_contact": settings.RMA_DEFAULT_TIANJIN_CONTACT,
            "shipping_phone": settings.RMA_DEFAULT_TIANJIN_PHONE,
            "shipping_postal_code": "",
        },
    )

    assert beijing == (
        "北京华峰测控技术股份有限公司\n"
        "北京市海淀区丰豪东路9号院5号楼\n"
        "李连荣电话：010-63725600-193；邮编：100094"
    )
    assert tianjin == (
        "华峰测控技术（天津）有限责任公司\n"
        "天津市滨海新区生态城川博道华峰测控1201号\n"
        "郭洋（收）  电话：022-67253518-8108"
    )
    assert replies._return_address_block(language="en-US") == settings.RMA_OVERSEAS_BEIJING_ADDRESS_BLOCK


def test_english_return_address_and_demi_signature_are_english_routed() -> None:
    block = replies._return_address_block(
        language="en-US",
        customer_policy={"shipping_address": "北京市海淀区丰豪东路9号院5号楼"},
    )
    demi = next(item for item in REPLY_TEMPLATES if item["template_type"] == "international_company_base")

    assert block == settings.RMA_OVERSEAS_BEIJING_ADDRESS_BLOCK
    assert "北京市" not in block
    assert "Demi Wang(王佳慧)" in demi["body_template"]
    assert "demi</span>.wang@accotest.com" in demi["html_body_template"]
    assert 'cid:accotest_logo' in demi["html_body_template"]


def test_domestic_warranty_routing_uses_sn_dates_and_rejects_mixed_status() -> None:
    out_type, out_version = replies._rma_reply_template_type(
        _ticket(language_code="zh-CN", request_date=date(2027, 1, 1))
    )
    assert (out_type, out_version) == (
        "rma_authorization_domestic_out_of_warranty", "domestic_out_warranty_v1"
    )

    mixed = _ticket(
        language_code="zh-CN",
        sn_validation_snapshot={"checks": [
            {"warranty_start_date": "2026-01-01", "warranty_end_date": "2026-12-31"},
            {"warranty_start_date": "2025-01-01", "warranty_end_date": "2025-12-31"},
        ]},
    )
    with pytest.raises(replies.RmaReplyRuleError, match="RMA_MIXED_WARRANTY_STATUS"):
        replies._rma_reply_template_type(mixed)


def test_domestic_out_of_warranty_template_renders_actual_price_and_city() -> None:
    template = next(
        item for item in REPLY_TEMPLATES
        if item["template_type"] == "rma_authorization_domestic_out_of_warranty"
    )
    ticket = _ticket(language_code="zh-CN", contact_person="刘家利")
    rendered = replies._render_template(
        template["body_template"], ticket=ticket, missing_fields=None,
        city="北京", repair_fee="1200.00", currency_unit="元",
        return_address_block="北京维修地址",
    )
    assert "寄到北京质量部" in rendered
    assert "维修费：1200.00元/块" in rendered
    assert "北京维修地址" in rendered


def test_overseas_out_of_warranty_and_special_rules() -> None:
    template_type, version = replies._rma_reply_template_type(
        _ticket(request_date=date(2027, 1, 1))
    )
    assert version == "overseas_out_warranty_v1"
    assert template_type == "rma_authorization_overseas_out_of_warranty"

    with pytest.raises(replies.RmaReplyRuleError) as st:
        replies._rma_reply_template_type(
            _ticket(customer_name="STMicroelectronics Pte Ltd", request_date=date(2026, 12, 31))
        )
    assert st.value.task_type == "rma_st_manual"
    assert st.value.reason == "RMA_ST_CUSTOM_HANDLING_REQUIRES_MANUAL"

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


def test_signature_logo_is_embedded_once_with_canonical_cid(monkeypatch) -> None:
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest1@accotest.com")
    reply = ReplyRecord(
        id=10, ticket_id=1, reply_type="rma_authorization",
        to_addresses="rmatest2@accotest.com", cc_addresses=None,
        subject="RMA logo", final_body="plain",
        final_html_body='<div><img src="cid:accotest_logo"></div>',
    )
    parsed = BytesParser(policy=policy.default).parsebytes(
        replies._build_reply_message(reply, "<logo@accotest.com>").as_bytes(policy=policy.SMTP)
    )
    logos = [part for part in parsed.walk() if str(part.get("Content-ID") or "") == "<accotest_logo>"]
    assert len(logos) == 1
    assert logos[0].get_content_type() == "image/png"


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
    assert preflight.result["rma_template_version"] == "rma_authorization_v3_2_reference"
    assert preflight.result["pdf_page_count"] == 3
    assert preflight.result["watermarked_page_count"] == 0
