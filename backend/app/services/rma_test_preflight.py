from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from typing import Any

import fitz

from app.config import settings
from app.models import ReplyRecord
from app.services.replies import _build_reply_message
from app.services.rma_pdf import RmaItemData, RmaPdfData, TEMPLATE_VERSION, render_rma_pdf, rma_pdf_file_name


TEST_SENDER = "rmatest1@accotest.com"
TEST_RECIPIENT = "rmatest2@accotest.com"


@dataclass(frozen=True)
class RmaTestPreflight:
    result: dict[str, Any]
    pdf_bytes: bytes
    mime_bytes: bytes


def build_rma_test_preflight(*, timestamp: str | None = None) -> RmaTestPreflight:
    """Build and inspect the authorized synthetic message without opening a network connection."""
    stamp = timestamp or datetime.now().strftime("%Y%m%d%H%M%S")
    authorization_no = f"TEST{stamp}"
    data = RmaPdfData(
        rma_no=authorization_no,
        request_date=date.today(),
        customer_code="RMATEST",
        customer_name="RMA SMTP TEST ONLY",
        mailing_address="TEST DATA - NO SHIPMENT",
        mailing_contact_person="TEST CONTACT",
        mailing_contact_phone="000-0000",
        delivery_fee_paid_by_customer="TEST ONLY",
        repair_fee_paid_by_customer="TEST ONLY",
        total_cost=Decimal("0"),
        items=[
            RmaItemData(
                part_no="TEST-PART",
                part_description="TEST ONLY / 测试数据",
                part_serial_no="TESTSN00000001",
                failure_description="SMTP attachment validation only / 非真实故障",
            )
        ],
    )
    pdf_bytes = render_rma_pdf(data, test_only=True)
    filename = rma_pdf_file_name(data)
    subject = f"[TEST ONLY] RMA授权表附件发送验证 RMATEST{stamp}"
    reply = ReplyRecord(
        ticket_id=0,
        reply_type="rma_authorization",
        to_addresses=TEST_RECIPIENT,
        cc_addresses=None,
        subject=subject,
        final_body=(
            "TEST ONLY / 测试数据\n"
            "This message validates one synthetic RMA PDF attachment. No real customer data is included."
        ),
    )
    message = _build_reply_message(
        reply,
        f"<rma-test-{stamp}@accotest.com>",
        attachment_content=pdf_bytes,
        attachment_filename=filename,
    )
    mime_bytes = message.as_bytes(policy=policy.SMTP)
    parsed = BytesParser(policy=policy.default).parsebytes(mime_bytes)
    attachments = list(parsed.iter_attachments())
    reasons: list[str] = []

    configured_sender = parseaddr(settings.SMTP_USER)[1].lower()
    if configured_sender != TEST_SENDER:
        reasons.append("SMTP_LOGIN_MUST_BE_RMATEST1")
    whitelist = {parseaddr(value)[1].lower() for value in settings.SMTP_RECIPIENT_WHITELIST if parseaddr(value)[1]}
    if whitelist != {TEST_RECIPIENT}:
        reasons.append("SMTP_WHITELIST_MUST_CONTAIN_ONLY_RMATEST2")
    if parseaddr(parsed.get("From", ""))[1].lower() != TEST_SENDER:
        reasons.append("MIME_FROM_MISMATCH")
    if [address.lower() for _, address in getaddresses(parsed.get_all("To", []))] != [TEST_RECIPIENT]:
        reasons.append("MIME_TO_MISMATCH")
    if parsed.get("Cc") is not None or parsed.get("Bcc") is not None:
        reasons.append("MIME_CC_BCC_MUST_BE_EMPTY")
    if len(attachments) != 1 or attachments[0].get_content_type() != "application/pdf":
        reasons.append("MIME_REQUIRES_EXACTLY_ONE_PDF")
    elif attachments[0].get_payload(decode=True) != pdf_bytes:
        reasons.append("MIME_PDF_HASH_MISMATCH")

    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        pdf_text = "\n".join(page.get_text() for page in document)
    compact_pdf_text = re.sub(r"\s+", "", pdf_text)
    for marker in ("TEST ONLY", "RMA SMTP TEST ONLY", "TESTSN00000001", "TEST-PART"):
        if re.sub(r"\s+", "", marker) not in compact_pdf_text:
            reasons.append(f"PDF_TEST_MARKER_MISSING:{marker}")

    pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
    attachment_hash = (
        hashlib.sha256(attachments[0].get_payload(decode=True)).hexdigest() if len(attachments) == 1 else None
    )
    return RmaTestPreflight(
        result={
            "status": "passed" if not reasons else "failed",
            "reasons": reasons,
            "network_connected": False,
            "send_count": 0,
            "from": TEST_SENDER,
            "to": TEST_RECIPIENT,
            "cc": [],
            "bcc": [],
            "subject": subject,
            "attachment_name": filename,
            "attachment_content_type": "application/pdf",
            "attachment_count": len(attachments),
            "attachment_size": len(pdf_bytes),
            "attachment_sha256": attachment_hash,
            "pdf_sha256": pdf_hash,
            "rma_template_version": TEMPLATE_VERSION,
            "smtp_login": configured_sender,
            "smtp_whitelist": sorted(whitelist),
        },
        pdf_bytes=pdf_bytes,
        mime_bytes=mime_bytes,
    )
