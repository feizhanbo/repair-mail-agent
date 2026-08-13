from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import replies


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_prepare_rma_explicitly_disables_smtp(monkeypatch) -> None:
    create = AsyncMock(return_value={"status": "prepared", "reply_id": 9})
    monkeypatch.setattr(replies, "create_and_send_rma_authorization", create)

    result = await replies.prepare_rma_authorization(
        SimpleNamespace(),
        ticket_id=1,
        user_id=None,
        expected_version=2,
        expected_safety_hash="a" * 64,
        expected_sn_validation_hash="b" * 64,
        expected_rma_template_version="v1",
        expected_rma_no="RMA1",
    )

    assert result["status"] == "prepared"
    assert create.await_args.kwargs["send_immediately"] is False


@pytest.mark.anyio
async def test_uncertain_prepared_reply_is_not_sent_again(monkeypatch) -> None:
    reply = SimpleNamespace(id=9, ticket_id=1, reply_type="rma_authorization", send_status="send_uncertain")
    monkeypatch.setattr(replies, "get_reply", AsyncMock(return_value=reply))
    send = AsyncMock()
    monkeypatch.setattr(replies, "_send_reply_record", send)

    result = await replies.send_prepared_rma_authorization(SimpleNamespace(), reply_id=9, user_id=None)

    assert result["status"] == "send_uncertain"
    assert result["idempotent_reuse"] is True
    send.assert_not_awaited()


@pytest.mark.anyio
async def test_prepared_reply_send_returns_persisted_evidence(monkeypatch) -> None:
    reply = SimpleNamespace(
        id=9,
        ticket_id=1,
        reply_type="rma_authorization",
        send_status="approved_pending_send",
        smtp_message_id=None,
        archive_status="not_required",
    )
    monkeypatch.setattr(replies, "get_reply", AsyncMock(return_value=reply))

    async def send(_session, *, reply, user_id, auto, finalize_rma):
        assert user_id is None and auto is True and finalize_rma is False
        reply.send_status = "sent"
        reply.smtp_message_id = "<stable@example.test>"
        reply.archive_status = "archived"

    monkeypatch.setattr(replies, "_send_reply_record", send)

    result = await replies.send_prepared_rma_authorization(SimpleNamespace(), reply_id=9, user_id=None)

    assert result == {
        "status": "sent",
        "ticket_id": 1,
        "reply_id": 9,
        "smtp_message_id": "<stable@example.test>",
        "archive_status": "archived",
        "idempotent_reuse": True,
    }


@pytest.mark.anyio
async def test_uncertain_non_rma_reply_is_not_sent_again(monkeypatch) -> None:
    reply = SimpleNamespace(id=11, ticket_id=2, reply_type="receipt", send_status="send_uncertain")
    monkeypatch.setattr(replies, "get_reply", AsyncMock(return_value=reply))
    send = AsyncMock()
    monkeypatch.setattr(replies, "_send_reply_record", send)

    result = await replies.send_prepared_reply(SimpleNamespace(), reply_id=11, user_id=None)

    assert result["status"] == "send_uncertain"
    assert result["idempotent_reuse"] is True
    send.assert_not_awaited()


def test_prepare_boundary_is_explicit_in_public_signature() -> None:
    import inspect

    signature = inspect.signature(replies.create_and_send_rma_authorization)
    assert signature.parameters["send_immediately"].default is True

    reply_signature = inspect.signature(replies.create_reply_draft)
    assert reply_signature.parameters["send_immediately"].default is True
