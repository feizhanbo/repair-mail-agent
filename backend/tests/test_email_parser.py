from __future__ import annotations

from app.models import Email
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

    assert intent == "customer_reply"
    assert confidence == 0.85
    assert extracted["items"][0]["sn"] == "15CN2240103920"
    assert extracted["fields"]["problem_description"] == "收到，RMA表已更新。"


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
