from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models import Email, EmailAttachment, RepairTicket, RepairTicketItem, ReplyRecord, ReplyTemplate
from app.seed import REPLY_TEMPLATES
from app.services import replies
from app.services.mail_reply_renderer import ReplyHistory


def _template(
    *,
    template_id: int,
    template_type: str,
    language: str = "zh-CN",
    body: str = "业务内容 {{ ticket_no }}",
) -> ReplyTemplate:
    return ReplyTemplate(
        id=template_id,
        template_code=f"{template_type}_{language}",
        template_name=template_type,
        template_type=template_type,
        language=language,
        version="v1",
        subject_template="Re: {{ original_subject }}",
        body_template=body,
        enabled=True,
    )


@pytest.mark.anyio
async def test_domestic_reply_composes_content_with_company_base(monkeypatch: pytest.MonkeyPatch) -> None:
    content = _template(template_id=1, template_type="receipt")
    base = _template(
        template_id=2,
        template_type="domestic_company_base",
        body="BASE-BEGIN\n{{ content }}\nBASE-END",
    )
    base.html_body_template = '<div>BASE-BEGIN {{ content }}<img src="cid:accotest_logo"></div>'
    ticket = RepairTicket(id=3, ticket_no="RMA-3", current_status_code="parsed")
    parent = Email(
        id=4,
        thread_id=5,
        mail_direction="inbound",
        mailbox_account="test",
        message_id="<parent@example.com>",
        subject="Original repair",
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=base))
    monkeypatch.setattr(
        replies,
        "render_reply_history",
        AsyncMock(
            return_value=ReplyHistory(
                plain="From: customer@example.com\n\nOriginal request",
                html="<blockquote><b>Original request</b></blockquote>",
                snapshot_hash="a" * 64,
                resources=(),
                raw_eml_sha256="b" * 64,
            )
        ),
    )

    subject, body, html_body, selected_base, history_hash, render_hash = await replies._render_reply_templates(
        session,
        content_template=content,
        ticket=ticket,
        missing_fields=None,
        parent=parent,
    )

    assert subject == "Re: Original repair"
    assert "Original request" in body
    assert body.startswith("BASE-BEGIN\n业务内容 RMA-3\nBASE-END")
    assert "业务内容 RMA-3" in html_body
    assert 'cid:accotest_logo' in html_body
    assert selected_base is base
    assert len(history_hash) == 64
    assert len(render_hash) == 64


@pytest.mark.anyio
async def test_reply_parent_must_be_inbound_and_in_same_thread() -> None:
    ticket = RepairTicket(
        id=1,
        ticket_no="RMA-1",
        current_status_code="parsed",
        source_email_id=10,
        thread_id=20,
    )
    outbound = Email(
        id=10,
        thread_id=20,
        mail_direction="outbound",
        mailbox_account="test",
        message_id="<outbound@example.com>",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=outbound), scalar=AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await replies._require_reply_parent(session, ticket=ticket, related_email_id=10)

    assert caught.value.status_code == 409
    assert caught.value.detail == "REPLY_PARENT_MUST_BE_INBOUND"


@pytest.mark.anyio
async def test_send_guard_requires_template_base_and_reply_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = RepairTicket(
        id=1,
        ticket_no="RMA-1",
        current_status_code="parsed",
        source_email_id=10,
        thread_id=20,
    )
    parent = Email(
        id=10,
        thread_id=20,
        mail_direction="inbound",
        mailbox_account="test",
        message_id="<parent@example.com>",
    )
    content = _template(template_id=30, template_type="receipt")
    base = _template(template_id=31, template_type="domestic_company_base")

    async def get(model, object_id, **_kwargs):
        if model is ReplyTemplate and object_id == 30:
            return content
        if model is ReplyTemplate and object_id == 31:
            return base
        if model is Email and object_id == 10:
            return parent
        return None

    history_hash = "a" * 64
    monkeypatch.setattr(
        replies,
        "render_reply_history",
        AsyncMock(
            return_value=ReplyHistory(
                plain="",
                html="",
                snapshot_hash=history_hash,
                resources=(),
                raw_eml_sha256="b" * 64,
            )
        ),
    )
    session = SimpleNamespace(get=get, scalar=AsyncMock())
    reply = ReplyRecord(
        ticket_id=1,
        related_email_id=10,
        template_id=30,
        base_template_id=31,
        reply_type="receipt",
        to_addresses="customer@example.com",
        subject="Re: Repair request RMA-1",
        draft_body="业务内容 RMA-1",
        final_body="业务内容 RMA-1",
        draft_html_body="<div>业务内容 RMA-1</div>",
        final_html_body="<div>业务内容 RMA-1</div>",
        thread_history_hash=history_hash,
        in_reply_to="<parent@example.com>",
        references_header="<root@example.com> <parent@example.com>",
    )
    reply.render_hash = replies._render_hash(
        subject=reply.subject,
        plain=reply.final_body,
        html_body=reply.final_html_body,
        history_hash=reply.thread_history_hash,
    )

    assert await replies._reply_send_guard_error(session, ticket=ticket, reply=reply) is None
    replies.render_reply_history.return_value = ReplyHistory(
        plain="changed",
        html="<div>changed</div>",
        snapshot_hash="c" * 64,
        resources=(),
        raw_eml_sha256="d" * 64,
    )
    assert (
        await replies._reply_send_guard_error(session, ticket=ticket, reply=reply)
        == "REPLY_THREAD_HISTORY_CHANGED_REGENERATE_REQUIRED"
    )
    replies.render_reply_history.return_value = ReplyHistory(
        plain="",
        html="",
        snapshot_hash=history_hash,
        resources=(),
        raw_eml_sha256="b" * 64,
    )
    reply.template_id = None
    assert await replies._reply_send_guard_error(session, ticket=ticket, reply=reply) == "REPLY_TEMPLATE_REQUIRED"


def test_seed_contains_all_runtime_reply_templates() -> None:
    keys = {(item["template_type"], item["language"]) for item in REPLY_TEMPLATES}
    assert ("domestic_company_base", "zh-CN") in keys
    assert ("international_company_base", "en-US") in keys
    for template_type in (
        "receipt",
        "missing_fields",
        "followup",
        "sn_invalid",
        "manual_review",
        "rma_attachment_disabled_receipt",
    ):
        assert (template_type, "zh-CN") in keys
        assert (template_type, "en-US") in keys
    assert ("rma_authorization_domestic_in_warranty", "zh-CN") in keys
    assert ("rma_authorization_domestic_out_of_warranty", "zh-CN") in keys
    assert ("rma_authorization_overseas_in_warranty", "en-US") in keys
    assert ("rma_authorization_overseas_out_of_warranty", "en-US") in keys
    assert ("rma_authorization_overseas_st_pickup", "en-US") in keys
    assert not any(item["template_type"] == "device_received_ack" for item in REPLY_TEMPLATES)


def test_seed_template_versions_fit_database_column() -> None:
    assert all(len(item["version"]) <= 30 for item in REPLY_TEMPLATES)


@pytest.mark.anyio
async def test_thread_history_uses_latest_segments_without_recursive_quoting() -> None:
    first = Email(
        id=1, thread_id=9, mail_direction="inbound", mailbox_account="test",
        message_id="<first@example.com>", from_address="customer@example.com",
        to_addresses="rmatest1@accotest.com", subject="Repair", latest_reply_segment="Original request",
        text_body="Original request\nOLD RECURSIVE QUOTE",
    )
    second = Email(
        id=2, thread_id=9, mail_direction="outbound", mailbox_account="test",
        message_id="<second@example.com>", from_address="rmatest1@accotest.com",
        to_addresses="rmatest2@accotest.com", subject="Re: Repair", latest_reply_segment="Please provide SN",
        text_body="Please provide SN\nOLD RECURSIVE QUOTE",
    )
    attachment = EmailAttachment(id=3, email_id=1, file_name="photo.jpg")

    class Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    session = SimpleNamespace(execute=AsyncMock(side_effect=[Result([first, second]), Result([attachment])]))
    ticket = RepairTicket(id=4, ticket_no="RMA-4", thread_id=9, current_status_code="parsed")

    plain, html_body, digest = await replies._thread_history(session, ticket=ticket, language="zh-CN")

    assert "Original request" in plain
    assert "Please provide SN" in plain
    assert "photo.jpg" in plain
    assert "OLD RECURSIVE QUOTE" not in plain
    assert "Original request" in html_body
    assert len(digest) == 64


def test_reply_message_is_true_reply_with_plain_and_html_alternatives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replies.settings, "SMTP_USER", "rmatest1@accotest.com")
    reply = ReplyRecord(
        id=8, ticket_id=2, reply_type="rma_authorization", to_addresses="rmatest2@accotest.com",
        subject="RMA2026080401上海林众电子科技有限公司",
        draft_body="RMA表格见附件。", final_body="RMA表格见附件。",
        draft_html_body="<div>RMA表格见附件。</div>", final_html_body="<div>RMA表格见附件。</div>",
        in_reply_to="<original@example.com>", references_header="<root@example.com> <original@example.com>",
    )

    message = replies._build_reply_message(reply, "<reply@example.com>")

    assert message["Subject"] == "[TEST ONLY] RMA2026080401上海林众电子科技有限公司"
    assert message["In-Reply-To"] == "<original@example.com>"
    assert message["References"] == "<root@example.com> <original@example.com>"
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == "RMA表格见附件。"
    assert "RMA表格见附件。" in message.get_body(preferencelist=("html",)).get_content()


@pytest.mark.anyio
async def test_followup_send_guard_blocks_stale_draft_after_information_is_complete() -> None:
    ticket = RepairTicket(
        id=1, ticket_no="RMA-1", current_status_code="need_customer_info",
        customer_name="客户", contact_person="联系人", contact_email="customer@example.com",
        contact_phone="13800000000", request_date="2026-08-05",
        mailing_address="测试地址", problem_description="故障",
    )
    item = RepairTicketItem(id=2, ticket_id=1, sn="SN001")

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [item]

    session = SimpleNamespace(execute=AsyncMock(return_value=Result()))
    reply = ReplyRecord(
        ticket_id=1, reply_type="missing_fields", to_addresses="rmatest2@accotest.com",
        missing_fields={"sn": "缺少设备 SN"},
    )

    assert await replies._reply_send_guard_error(session, ticket=ticket, reply=reply) == "FOLLOWUP_NO_LONGER_REQUIRED"


@pytest.mark.anyio
async def test_followup_send_guard_blocks_when_missing_field_snapshot_changed() -> None:
    ticket = RepairTicket(
        id=1, ticket_no="RMA-1", current_status_code="need_customer_info",
        customer_name=None, contact_person="联系人", contact_email="customer@example.com",
        request_date="2026-08-05", mailing_address="测试地址", problem_description="故障",
    )
    item = RepairTicketItem(id=2, ticket_id=1, sn="SN001")

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [item]

    session = SimpleNamespace(execute=AsyncMock(return_value=Result()))
    reply = ReplyRecord(
        ticket_id=1, reply_type="missing_fields", to_addresses="rmatest2@accotest.com",
        missing_fields={"sn": "缺少设备 SN"},
    )

    assert await replies._reply_send_guard_error(session, ticket=ticket, reply=reply) == "FOLLOWUP_MISSING_FIELDS_CHANGED_REGENERATE_REQUIRED"


@pytest.mark.anyio
async def test_reply_subject_and_body_cannot_be_edited_outside_template(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = ReplyRecord(
        id=1,
        ticket_id=2,
        reply_type="receipt",
        to_addresses="customer@example.com",
        review_status="pending",
        send_status="pending_review",
    )
    monkeypatch.setattr(replies, "get_reply", AsyncMock(return_value=reply))

    with pytest.raises(HTTPException) as caught:
        await replies.update_reply(
            SimpleNamespace(),
            reply_id=1,
            user_id=3,
            values={"final_body": "绕过模板的正文"},
        )

    assert caught.value.detail == "REPLY_FIELD_NOT_ALLOWED:final_body"
