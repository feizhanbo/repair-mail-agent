from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models import Email, RepairTicket, ReplyRecord, ReplyTemplate
from app.seed import REPLY_TEMPLATES
from app.services import replies


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
async def test_domestic_reply_composes_content_with_company_base() -> None:
    content = _template(template_id=1, template_type="receipt")
    base = _template(
        template_id=2,
        template_type="domestic_company_base",
        body="BASE-BEGIN\n{{ content }}\nBASE-END",
    )
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

    subject, body, selected_base = await replies._render_reply_templates(
        session,
        content_template=content,
        ticket=ticket,
        missing_fields=None,
        parent=parent,
    )

    assert subject == "Re: Original repair"
    assert body == "BASE-BEGIN\n业务内容 RMA-3\nBASE-END"
    assert selected_base is base


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
async def test_send_guard_requires_template_base_and_reply_headers() -> None:
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
        in_reply_to="<parent@example.com>",
        references_header="<root@example.com> <parent@example.com>",
    )

    assert await replies._reply_send_guard_error(session, ticket=ticket, reply=reply) is None
    reply.template_id = None
    assert await replies._reply_send_guard_error(session, ticket=ticket, reply=reply) == "REPLY_TEMPLATE_REQUIRED"


def test_seed_contains_all_runtime_reply_templates() -> None:
    keys = {(item["template_type"], item["language"]) for item in REPLY_TEMPLATES}
    assert ("domestic_company_base", "zh-CN") in keys
    for template_type in (
        "receipt",
        "missing_fields",
        "followup",
        "sn_invalid",
        "manual_review",
        "rma_attachment_disabled_receipt",
        "device_received_ack",
    ):
        assert (template_type, "zh-CN") in keys
        assert (template_type, "en-US") in keys
    assert ("rma_authorization_domestic", "zh-CN") in keys
    assert ("rma_authorization_overseas_in_warranty", "en-US") in keys
    assert ("rma_authorization_overseas_out_of_warranty", "en-US") in keys
    assert ("rma_authorization_overseas_st_pickup", "en-US") in keys


def test_seed_template_versions_fit_database_column() -> None:
    assert all(len(item["version"]) <= 30 for item in REPLY_TEMPLATES)


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
