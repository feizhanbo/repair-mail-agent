from __future__ import annotations

import socket
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EmailOutbox, ReplyRecord
from app.services.common import utcnow


IMMUTABLE_OUTBOX_STATUSES = {
    "ready", "claimed", "failed_retryable", "sending", "accepted", "partial_accepted", "uncertain"
}
CLAIMABLE_OUTBOX_STATUSES = {"ready", "failed_retryable"}


class OutboxStateError(RuntimeError):
    pass


async def prepare_outbox(
    session: AsyncSession,
    *,
    reply: ReplyRecord,
    frozen_eml_oss_object_id: int,
    frozen_eml_sha256: str,
    message_id: str,
    from_address: str,
    ticket_version: int | None,
    request_id: str | None = None,
    rma_no: str | None = None,
    pdf_sha256: str | None = None,
    safety_snapshot: dict | None = None,
) -> EmailOutbox:
    existing = await session.scalar(
        select(EmailOutbox).where(EmailOutbox.reply_record_id == reply.id).with_for_update()
    )
    if existing is not None and existing.status in IMMUTABLE_OUTBOX_STATUSES:
        if existing.frozen_eml_sha256 != frozen_eml_sha256 or existing.message_id != message_id:
            raise OutboxStateError("OUTBOX_IMMUTABLE_CONTENT_MISMATCH")
        return existing
    values = {
        "ticket_id": reply.ticket_id,
        "related_email_id": reply.related_email_id,
        "frozen_eml_oss_object_id": frozen_eml_oss_object_id,
        "idempotency_key": f"reply:{reply.id}:smtp",
        "message_id": message_id,
        "from_address": from_address,
        "to_addresses": reply.to_addresses,
        "cc_addresses": reply.cc_addresses,
        "subject": reply.subject or "",
        "frozen_eml_sha256": frozen_eml_sha256,
        "status": "ready",
        "ticket_version": ticket_version,
        "thread_history_hash": reply.thread_history_hash,
        "request_id": request_id,
        "rma_no": rma_no,
        "pdf_sha256": pdf_sha256,
        "template_version": reply.rma_template_version or reply.reply_template_version,
        "safety_snapshot": safety_snapshot,
        "last_error_code": None,
        "last_error_message": None,
    }
    if existing is None:
        existing = EmailOutbox(reply_record_id=reply.id, **values)
        session.add(existing)
    else:
        for key, value in values.items():
            setattr(existing, key, value)
    await session.flush()
    return existing


async def claim_next_outbox(
    session: AsyncSession,
    *,
    worker_id: str | None = None,
    lease_seconds: int = 300,
) -> EmailOutbox | None:
    now = utcnow()
    row = await session.scalar(
        select(EmailOutbox)
        .where(
            EmailOutbox.status.in_(CLAIMABLE_OUTBOX_STATUSES),
            or_(EmailOutbox.next_attempt_at.is_(None), EmailOutbox.next_attempt_at <= now),
            or_(EmailOutbox.lease_expires_at.is_(None), EmailOutbox.lease_expires_at <= now),
        )
        .order_by(EmailOutbox.created_at.asc(), EmailOutbox.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    row.status = "claimed"
    row.lease_owner = (worker_id or socket.gethostname())[:100]
    row.lease_expires_at = now + timedelta(seconds=max(30, lease_seconds))
    row.attempt_count = int(row.attempt_count or 0) + 1
    await session.flush()
    return row


def mark_outbox_sending(outbox: EmailOutbox) -> None:
    if outbox.status not in {"ready", "failed_retryable", "claimed"}:
        raise OutboxStateError("OUTBOX_NOT_SENDABLE")
    outbox.status = "sending"


def mark_outbox_accepted(outbox: EmailOutbox, *, smtp_response: str) -> None:
    outbox.status = "accepted"
    outbox.smtp_response = smtp_response
    outbox.accepted_at = utcnow()
    outbox.lease_owner = None
    outbox.lease_expires_at = None
    outbox.next_attempt_at = None
    outbox.last_error_code = None
    outbox.last_error_message = None


def mark_outbox_failed(
    outbox: EmailOutbox,
    *,
    error_code: str,
    uncertain: bool,
    partial_accepted: bool = False,
    retryable: bool = True,
) -> None:
    outbox.status = (
        "partial_accepted"
        if partial_accepted
        else ("uncertain" if uncertain else ("failed_retryable" if retryable else "failed_terminal"))
    )
    outbox.last_error_code = error_code
    outbox.last_error_message = error_code
    outbox.lease_owner = None
    outbox.lease_expires_at = None
