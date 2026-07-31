from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RepairTicket
from app.services.audit import log_operation
from app.services.common import utcnow


async def confirm_device_received(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_id: int | None,
    source: str,
    source_email_id: int | None = None,
    note: str | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Record a physical-receipt fact without advancing the RMA workflow.

    RMA issuance is complete once the formal number, validated PDF, SMTP
    acceptance, Message-ID, and archives are verified.  A later device receipt
    is therefore an audit event only: it cannot send another automatic reply,
    close/reopen a ticket, or bypass classification of a new inbound email.
    """
    ticket = await session.get(RepairTicket, ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="TICKET_NOT_FOUND",
        )

    idempotent_reuse = ticket.device_received_idempotency_key == idempotency_key
    if ticket.device_received_at is None:
        ticket.device_received_at = utcnow()
        ticket.device_received_source = source
        ticket.device_received_email_id = source_email_id
        ticket.device_received_note = note
        ticket.device_received_idempotency_key = idempotency_key
    elif note and not ticket.device_received_note:
        ticket.device_received_note = note

    ticket.device_receipt_ack_status = (
        "not_required_after_rma_issue"
        if ticket.current_status_code == "closed"
        else "recorded_no_progression"
    )
    await log_operation(
        session,
        user_id=user_id,
        operation_type="device_received_recorded",
        target_type="repair_ticket",
        target_id=ticket.id,
        email_id=source_email_id,
        ticket_id=ticket.id,
        after_data={
            "source": source,
            "ticket_status": ticket.current_status_code,
            "ack_status": ticket.device_receipt_ack_status,
            "idempotent_reuse": idempotent_reuse,
        },
    )
    return {
        "ticket_id": ticket.id,
        "status": (
            "recorded_after_rma_issue"
            if ticket.current_status_code == "closed"
            else "recorded_no_progression"
        ),
        "reply_id": None,
        "idempotent_reuse": idempotent_reuse,
    }
