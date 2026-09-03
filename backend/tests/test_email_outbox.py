from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models import EmailOutbox
from app.services.email_outbox import OutboxStateError, mark_outbox_failed, prepare_outbox


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _reply() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        ticket_id=8,
        related_email_id=9,
        to_addresses="customer@example.com",
        cc_addresses=None,
        subject="Re: repair",
        thread_history_hash="b" * 64,
        rma_template_version="rma-v1",
        reply_template_version="reply-v1",
    )


class Session:
    def __init__(self, existing=None) -> None:
        self.existing = existing
        self.added = []

    async def scalar(self, _statement):
        return self.existing

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        for row in self.added:
            row.id = row.id or 1


@pytest.mark.anyio
async def test_prepare_outbox_freezes_business_and_transport_snapshot() -> None:
    session = Session()
    outbox = await prepare_outbox(
        session,
        reply=_reply(),
        frozen_eml_oss_object_id=11,
        frozen_eml_sha256="a" * 64,
        message_id="<reply-7@example.com>",
        from_address="repair@example.com",
        ticket_version=3,
        request_id="11111111-1111-4111-8111-111111111111",
        rma_no="RMA-001",
        pdf_sha256="c" * 64,
        safety_snapshot={"sap_request_ids": ["11111111-1111-4111-8111-111111111111"]},
    )

    assert isinstance(outbox, EmailOutbox)
    assert outbox.status == "ready"
    assert outbox.idempotency_key == "reply:7:smtp"
    assert outbox.pdf_sha256 == "c" * 64
    assert outbox.safety_snapshot["sap_request_ids"]


@pytest.mark.anyio
async def test_ready_outbox_rejects_frozen_mime_drift() -> None:
    existing = EmailOutbox(
        id=1,
        reply_record_id=7,
        ticket_id=8,
        frozen_eml_oss_object_id=11,
        idempotency_key="reply:7:smtp",
        message_id="<reply-7@example.com>",
        from_address="repair@example.com",
        to_addresses="customer@example.com",
        subject="Re: repair",
        frozen_eml_sha256="a" * 64,
        status="ready",
    )

    with pytest.raises(OutboxStateError, match="OUTBOX_IMMUTABLE_CONTENT_MISMATCH"):
        await prepare_outbox(
            Session(existing),
            reply=_reply(),
            frozen_eml_oss_object_id=12,
            frozen_eml_sha256="d" * 64,
            message_id="<reply-7@example.com>",
            from_address="repair@example.com",
            ticket_version=3,
        )


def test_terminal_smtp_rejection_is_not_left_retryable() -> None:
    outbox = SimpleNamespace(status="sending", lease_owner="worker", lease_expires_at=object())
    mark_outbox_failed(
        outbox,
        error_code="SMTP_REJECTED_TERMINAL",
        uncertain=False,
        retryable=False,
    )
    assert outbox.status == "failed_terminal"
    assert outbox.lease_owner is None
    assert outbox.lease_expires_at is None
