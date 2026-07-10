from __future__ import annotations

from email.message import EmailMessage

import pytest

from app.services.eml import attachment_blobs_from_eml_bytes, payload_from_eml_bytes
from tools.verify_email_flow import _offline_report, parse_args


def test_payload_from_eml_extracts_core_mail_fields(tmp_path) -> None:
    eml = tmp_path / "sample.eml"
    message = EmailMessage()
    message["From"] = "Customer <customer@example.com>"
    message["To"] = "repair@example.com"
    message["Cc"] = "leader@example.com"
    message["Subject"] = "报修 SN: SN202607100001"
    message["Message-ID"] = "<sample-001@example.com>"
    message["In-Reply-To"] = "<root@example.com>"
    message["References"] = "<root@example.com>"
    message["Date"] = "Fri, 10 Jul 2026 09:30:00 +0800"
    message.set_content("设备 SN: SN202607100001\n故障现象：无法开机。\n联系电话：13800138000")
    message.add_attachment("附件补充说明", subtype="plain", filename="note.txt")
    eml.write_bytes(message.as_bytes())

    payload = payload_from_eml_bytes(eml.read_bytes(), mailbox_account="qa-mailbox", folder_name="INBOX")

    assert payload.mailbox_account == "qa-mailbox"
    assert payload.folder_name == "INBOX"
    assert payload.message_id == "<sample-001@example.com>"
    assert payload.in_reply_to == "<root@example.com>"
    assert payload.references_header == "<root@example.com>"
    assert payload.from_address == "customer@example.com"
    assert payload.to_addresses == "repair@example.com"
    assert payload.cc_addresses == "leader@example.com"
    assert payload.subject == "报修 SN: SN202607100001"
    assert payload.sent_at is not None
    assert "SN202607100001" in (payload.text_body or "")
    assert payload.attachments[0]["file_name"] == "note.txt"
    assert payload.attachments[0]["parse_status"] == "parsed"
    blobs = attachment_blobs_from_eml_bytes(eml.read_bytes())
    assert blobs[0]["file_name"] == "note.txt"
    assert blobs[0]["file_hash"] == payload.attachments[0]["file_hash"]
    assert len(blobs[0]["content"]) == payload.attachments[0]["file_size"]
    assert payload.attachments[0]["extracted_text"] == "附件补充说明"


def test_email_id_trace_mode_does_not_require_write_confirmation() -> None:
    args = parse_args(["--email-id", "42"])

    assert args.email_id == 42
    assert args.confirm_write is False


def test_reparse_existing_requires_write_confirmation() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--email-id", "42", "--reparse-existing"])

    args = parse_args(["--email-id", "42", "--reparse-existing", "--confirm-write"])
    assert args.email_id == 42
    assert args.reparse_existing is True
    assert args.confirm_write is True


def test_write_modes_require_explicit_confirmation(tmp_path) -> None:
    sample = tmp_path / "sample.json"
    sample.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit):
        parse_args(["--sample", str(sample)])

    args = parse_args(["--sample", str(sample), "--confirm-write"])
    assert args.sample == sample
    assert args.confirm_write is True


def test_dry_run_mode_does_not_require_write_confirmation(tmp_path) -> None:
    eml = tmp_path / "sample.eml"
    eml.write_text("From: customer@example.com\nSubject: 报修 SN: SN202607100001\n\n故障现象：无法开机", encoding="utf-8")

    args = parse_args(["--eml", str(eml), "--dry-run"])

    assert args.eml == eml
    assert args.dry_run is True
    assert args.confirm_write is False


def test_offline_report_includes_rule_parse_without_secret(tmp_path) -> None:
    eml = tmp_path / "sample.eml"
    eml.write_text(
        "From: Customer <customer@example.com>\n"
        "Subject: 报修 SN: SN202607100001\n"
        "\n"
        "设备 SN: SN202607100001\n故障现象：无法开机。\n联系电话：13800138000",
        encoding="utf-8",
    )
    payload = payload_from_eml_bytes(eml.read_bytes())

    report = _offline_report(payload)

    assert report["mode"] == "dry_run"
    assert report["writes_database"] is False
    assert report["runtime_config"]["ai_configured"] in {True, False}
    assert "sk-" not in str(report).lower()
    assert "bert123456" not in str(report)
    assert report["rule_classification"]["intent_type"] == "new_repair"
    assert report["rule_parse"]["items"][0]["sn"] == "SN202607100001"
