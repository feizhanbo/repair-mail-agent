from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Email
from app.services.eml import payload_from_eml_bytes
from app.services.parser import (
    classify_email,
    extract_fields,
    extract_latest_reply_segment,
    html_to_text,
    normalize_email_body,
)


def test_latest_reply_stops_before_signature_and_history() -> None:
    body = (
        "郭洋：\n收到\n连荣：RMA表已更新，请见附件，谢谢\n\n"
        "Best Regards!\nMiya Fang\n发件人：客户\nSN：15CN2240103920 故障"
    )

    latest = extract_latest_reply_segment(body)

    assert latest == "郭洋：\n收到\n连荣：RMA表已更新，请见附件，谢谢"


def test_reply_references_drive_classification_while_history_supplies_sn() -> None:
    email = Email(
        mailbox_account="manual-eml",
        message_id="<reply@example.com>",
        references_header="<root@example.com>",
        from_address="sender@example.com",
        subject="RMA update",
        clean_body="收到，RMA表已更新。\n发件人：客户\nSN：15CN2240103920 高频源损坏",
        latest_reply_segment="收到，RMA表已更新。",
    )

    intent, confidence, _reason = classify_email(email, email.latest_reply_segment or "")
    extracted = extract_fields(email)

    assert intent == "repair_thread_other"
    assert confidence == 0.78
    assert extracted["items"][0]["sn"] == "15CN2240103920"
    assert extracted["fields"]["problem_description"] == "高频源损坏"


def test_rebuilding_conversation_keeps_history_after_latest_reply_was_saved() -> None:
    email = Email(
        mailbox_account="manual-eml",
        message_id="<reply@example.com>",
        from_address="customer@example.com",
        subject="Re: RMA",
        text_body="收到\n\nFrom: service@example.com\n历史 SN: 15CN2240103920",
        clean_body="收到",
        latest_reply_segment="收到",
    )
    conversation = normalize_email_body(email.text_body or html_to_text(email.html_body) or email.clean_body)
    email.clean_body = conversation
    email.latest_reply_segment = extract_latest_reply_segment(conversation)
    extracted = extract_fields(email)

    assert email.latest_reply_segment == "收到"
    assert extracted["items"][0]["sn"] == "15CN2240103920"

def test_rule_parser_does_not_extract_phone_candidate_fields() -> None:
    email = Email(
        mailbox_account="manual-eml",
        message_id="<phone@example.com>",
        from_address="customer@example.com",
        subject="RMA SN: 15CN2240103920",
        clean_body="故障：无法开机\n联系电话：13800138000\nSN: 15CN2240103920",
    )

    extracted = extract_fields(email)

    assert "contact_phone" not in extracted["fields"]
    assert "contact_phone" not in extracted["missing_fields"]
    assert "contact_phone" not in extracted["field_confidences"]


GIVEN_REPLY_EML = Path(r"D:\refile\testdata\test\回复_ RMA2026070903通富微电子股份有限公司.eml")


@pytest.mark.skipif(not GIVEN_REPLY_EML.is_file(), reason="user-provided EML fixture is not available")
def test_given_reply_eml_recovers_thread_history_without_creating_a_new_repair() -> None:
    payload = payload_from_eml_bytes(GIVEN_REPLY_EML.read_bytes(), mailbox_account="offline-test")
    email = Email(
        mailbox_account=payload.mailbox_account,
        message_id=payload.message_id,
        references_header=payload.references_header,
        in_reply_to=payload.in_reply_to,
        from_address=payload.from_address,
        subject=payload.subject,
        text_body=payload.text_body,
        clean_body=normalize_email_body(payload.text_body),
    )
    email.latest_reply_segment = extract_latest_reply_segment(email.clean_body)

    intent, _, _ = classify_email(email, email.latest_reply_segment)
    extracted = extract_fields(email)

    assert payload.references_header and "<4675f26c4ff842268b19536529c4e154@tfme.com>" in payload.references_header
    assert intent == "device_intake_received"
    assert "M81231701100057" in {item["sn"] for item in extracted["items"]}
    assert "测试值异常" in extracted["fields"]["problem_description"]
