from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExternalOperationRecord
from app.services.common import utcnow


TERMINAL_OPERATION_STATUSES = {"succeeded", "failed_terminal"}


async def get_external_operation(
    session: AsyncSession,
    *,
    operation_type: str,
    operation_key: str,
    for_update: bool = False,
) -> ExternalOperationRecord | None:
    statement = select(ExternalOperationRecord).where(
        ExternalOperationRecord.operation_type == operation_type,
        ExternalOperationRecord.operation_key == operation_key,
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def start_external_operation(
    session: AsyncSession,
    *,
    operation_type: str,
    operation_key: str,
    ticket_id: int | None = None,
    email_id: int | None = None,
    reply_record_id: int | None = None,
    export_sap_id: int | None = None,
    recovery_stage: str | None = None,
    details: dict[str, Any] | None = None,
) -> ExternalOperationRecord:
    record = await get_external_operation(
        session,
        operation_type=operation_type,
        operation_key=operation_key,
        for_update=True,
    )
    if record is None:
        record = ExternalOperationRecord(
            operation_type=operation_type,
            operation_key=operation_key,
            ticket_id=ticket_id,
            email_id=email_id,
            reply_record_id=reply_record_id,
            export_sap_id=export_sap_id,
        )
        session.add(record)
        await session.flush()
    if record.status == "succeeded":
        return record
    record.status = "running"
    # SQLAlchemy column defaults are normally applied during INSERT.  Keeping
    # this defensive also makes the ledger safe for in-memory/unit-of-work
    # sessions that do not round-trip the inserted row.
    record.attempt_count = int(record.attempt_count or 0) + 1
    record.started_at = utcnow()
    record.completed_at = None
    record.error_code = None
    record.error_message = None
    record.retryable = True
    record.recovery_stage = recovery_stage
    record.next_retry_at = None
    if details is not None:
        record.details_json = details
    return record


def succeed_external_operation(
    record: ExternalOperationRecord,
    *,
    remote_reference: str | None = None,
    details: dict[str, Any] | None = None,
) -> ExternalOperationRecord:
    record.status = "succeeded"
    record.remote_reference = remote_reference or record.remote_reference
    record.error_code = None
    record.error_message = None
    record.retryable = False
    record.recovery_stage = None
    record.next_retry_at = None
    record.completed_at = utcnow()
    if details is not None:
        record.details_json = details
    return record


def fail_external_operation(
    record: ExternalOperationRecord,
    *,
    error_code: str,
    error_message: str | None = None,
    retryable: bool,
    uncertain: bool = False,
    recovery_stage: str | None = None,
    next_retry_at: Any | None = None,
    details: dict[str, Any] | None = None,
) -> ExternalOperationRecord:
    if uncertain:
        record.status = "uncertain"
    elif retryable:
        record.status = "failed_retryable"
    else:
        record.status = "failed_terminal"
    record.error_code = error_code
    record.error_message = error_message
    record.retryable = retryable
    record.recovery_stage = recovery_stage
    record.next_retry_at = next_retry_at
    record.completed_at = utcnow()
    if details is not None:
        record.details_json = details
    return record
