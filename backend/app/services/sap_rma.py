from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Email,
    ExportSap,
    ManualReviewTask,
    RepairTicket,
    RepairTicketItem,
    TicketRelayExport,
    TicketRma,
    TicketRmaItem,
)
from app.services.common import model_to_dict, utcnow
from app.services.business_notifications import notify_ticket_once
from app.services.external_operations import (
    fail_external_operation,
    start_external_operation,
    succeed_external_operation,
)
from app.integrations.sap_middleware import (
    ExternalRmaSubmissionItem,
    SapUnknownCommitStateError,
    create_sap_middleware_adapter,
)
from app.services.jobs import enqueue_job
from app.services.rma_pdf import TEMPLATE_VERSION as RMA_TEMPLATE_VERSION
from app.services.workflow import create_manual_task_if_missing, transition_ticket


RMA_PATTERN = re.compile(r"^\d{10}$")
EXPORT_FIELDS = (
    "id",
    "ticket_id",
    "ticket_item_id",
    "relay_export_id",
    "ticket_version",
    "source_request_id",
    "payload_hash",
    "policy_snapshot",
    "status",
    "attempt_count",
    "remote_call_id",
    "rma_no",
    "last_error_code",
    "last_error_message",
    "next_retry_at",
    "submitted_at",
    "accepted_at",
    "last_polled_at",
    "rma_received_at",
    "sn",
    "customer_code",
    "material_code",
    "customer_name",
    "material_name",
    "charge_status",
    "contact_person",
    "contact_phone",
    "email_subject",
    "problem_description",
    "repair_requested_at",
    "mailing_address",
    "currency",
    "shipping_fee",
    "repair_fee",
    "tax_rate",
    "created_at",
    "updated_at",
)
RMA_FIELDS = (
    "id",
    "ticket_id",
    "rma_no",
    "customer_code",
    "repair_business_date",
    "status",
    "policy_snapshot",
    "pdf_oss_object_id",
    "reply_record_id",
    "received_at",
    "sent_at",
    "created_at",
    "updated_at",
)


def serialize_export(row: ExportSap) -> dict[str, Any]:
    return model_to_dict(row, EXPORT_FIELDS)


def serialize_rma(row: TicketRma) -> dict[str, Any]:
    return model_to_dict(row, RMA_FIELDS)


def validate_rma_no(value: str) -> str:
    normalized = value.strip()
    if not RMA_PATTERN.fullmatch(normalized):
        raise ValueError("RMA_NUMBER_FORMAT_INVALID")
    try:
        datetime.strptime(normalized[:8], "%Y%m%d")
    except ValueError as exc:
        raise ValueError("RMA_NUMBER_DATE_INVALID") from exc
    if normalized[-2:] == "00":
        raise ValueError("RMA_NUMBER_SEQUENCE_INVALID")
    return normalized


def _stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _source_request_id() -> str:
    return str(uuid.uuid4())


async def _move_to_manual(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    task_type: str,
    reason: str,
) -> dict[str, Any]:
    ticket.rma_status = "manual_review"
    if ticket.current_status_code != "manual_review":
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="manual_review",
            trigger_event="manual_review_required",
            user_id=None,
            reason=reason,
            manual_task_type=task_type,
            manual_task_priority="high",
        )
    else:
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type=task_type,
            trigger_reason=reason,
            priority="high",
            assigned_user_id=ticket.assigned_user_id,
        )
    return {
        "status": "manual_review",
        "error_code": reason,
        "ticket_id": ticket.id,
    }


async def ensure_export_lines(
    session: AsyncSession,
    *,
    export: TicketRelayExport,
    ticket: RepairTicket,
) -> list[ExportSap]:
    existing = (
        await session.execute(
            select(ExportSap)
            .where(ExportSap.relay_export_id == export.id)
            .order_by(ExportSap.ticket_item_id)
        )
    ).scalars().all()
    if existing:
        return list(existing)

    items = (
        await session.execute(
            select(RepairTicketItem)
            .where(RepairTicketItem.ticket_id == ticket.id)
            .order_by(RepairTicketItem.line_no, RepairTicketItem.id)
        )
    ).scalars().all()
    if not items:
        raise ValueError("SAP_EXPORT_ITEMS_REQUIRED")
    source_email = await session.get(Email, ticket.source_email_id) if ticket.source_email_id else None
    requested_on = ticket.request_date or utcnow().date()
    prepared: list[tuple[RepairTicketItem, dict[str, Any], str]] = []

    for item in items:
        sn = (item.sn or "").strip().upper()
        material_code = (item.material_code or "").strip()
        if (
            not sn
            or not ticket.customer_code
            or not material_code
            or not item.material_name
            or not ticket.mailing_address
            or not ticket.contact_person
            or not ticket.contact_phone
            or ticket.charge_status not in {"free", "annual_contract", "chargeable"}
            or ticket.policy_resolution_status != "resolved"
        ):
            raise ValueError("SAP_EXPORT_REQUIRED_FIELDS_MISSING")
        policy = dict(ticket.policy_snapshot or {})
        policy["charge_status"] = ticket.charge_status
        policy["customer_scope"] = ticket.customer_scope
        # RMA rendering consumes these compatibility keys, but SAP payload
        # fields below always come from the customer's mailing information.
        policy["shipping_route"] = item.return_location
        policy["shipping_address"] = item.return_address
        policy["shipping_contact"] = item.return_contact
        policy["shipping_phone"] = item.return_phone
        policy["shipping_postal_code"] = item.return_postal_code
        policy["return_route_source"] = item.return_route_source
        policy["return_route_status"] = item.return_route_status
        policy["return_route_snapshot"] = item.return_route_snapshot
        prepared.append((item, policy, sn))

    rows: list[ExportSap] = []
    for item, policy, sn in prepared:
        payload = {
            "sn": sn,
            "customer_code": ticket.customer_code,
            "material_code": item.material_code,
            "customer_name": ticket.customer_name,
            "material_name": item.material_name,
            "charge_status": ticket.charge_status,
            "contact_person": ticket.contact_person,
            "contact_phone": ticket.contact_phone,
            "email_subject": source_email.subject if source_email else None,
            "problem_description": item.failure_description or ticket.problem_description,
            "repair_requested_at": requested_on.isoformat(),
            "mailing_address": ticket.mailing_address,
            "currency": policy["currency"],
            "shipping_fee": policy["shipping_fee_text"],
            "repair_fee": policy["repair_price"],
            "tax_rate": policy["tax_rate"],
        }
        payload_hash = _stable_hash({"payload": payload, "policy_snapshot": policy})
        existing_row = await session.scalar(
            select(ExportSap).where(
                ExportSap.ticket_item_id == item.id,
                ExportSap.ticket_version == ticket.version,
                ExportSap.payload_hash == payload_hash,
            )
        )
        if existing_row is not None:
            existing_row.relay_export_id = export.id
            if existing_row.status not in {"waiting_sap_result", "waiting_rma", "rma_received"}:
                existing_row.status = "pending"
                existing_row.last_error_code = None
                existing_row.last_error_message = None
                existing_row.next_retry_at = None
            rows.append(existing_row)
            continue
        row = ExportSap(
            ticket_id=ticket.id,
            ticket_item_id=item.id,
            relay_export_id=export.id,
            ticket_version=ticket.version,
            source_request_id=_source_request_id(),
            payload_hash=payload_hash,
            policy_snapshot=policy,
            status="pending",
            sn=payload["sn"],
            customer_code=payload["customer_code"],
            material_code=payload["material_code"],
            customer_name=payload["customer_name"],
            material_name=payload["material_name"],
            charge_status=payload["charge_status"],
            contact_person=payload["contact_person"],
            contact_phone=payload["contact_phone"],
            email_subject=payload["email_subject"],
            problem_description=payload["problem_description"],
            repair_requested_at=datetime.combine(requested_on, time.min),
            mailing_address=payload["mailing_address"],
            currency=payload["currency"],
            shipping_fee=payload["shipping_fee"],
            repair_fee=Decimal(str(payload["repair_fee"])),
            tax_rate=Decimal(str(payload["tax_rate"])),
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


def _line_payload(row: ExportSap) -> dict[str, Any]:
    return {
        "source_request_id": row.source_request_id,
        "ticket_id": row.ticket_id,
        "ticket_item_id": row.ticket_item_id,
        "relay_export_id": row.relay_export_id,
        "sn": row.sn,
        "customer_code": row.customer_code,
        "material_code": row.material_code,
        "customer_name": row.customer_name,
        "material_name": row.material_name,
        "charge_status": row.charge_status,
        "contact_person": row.contact_person,
        "contact_phone": row.contact_phone,
        "email_subject": row.email_subject,
        "problem_description": row.problem_description,
        "repair_requested_at": (
            row.repair_requested_at.isoformat()
            if row.repair_requested_at is not None
            else None
        ),
        "mailing_address": row.mailing_address,
        "currency": row.currency,
        "shipping_fee": row.shipping_fee,
        "repair_fee": str(row.repair_fee) if row.repair_fee is not None else None,
        "tax_rate": str(row.tax_rate) if row.tax_rate is not None else None,
    }


async def submit_export_batch(
    session: AsyncSession,
    *,
    export_id: int,
    schedule_jobs: bool = True,
) -> dict[str, Any]:
    export = await session.get(TicketRelayExport, export_id, with_for_update=True)
    if export is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RELAY_EXPORT_NOT_FOUND")
    ticket = await session.get(RepairTicket, export.ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")
    if ticket.version != export.ticket_version or ticket.safety_check_hash != export.payload_hash:
        export.status = "superseded"
        export.error_code = "TICKET_SNAPSHOT_SUPERSEDED"
        return {"status": "superseded", "export_id": export.id}
    try:
        lines = await ensure_export_lines(session, export=export, ticket=ticket)
    except ValueError as exc:
        export.status = "manual_review"
        export.error_code = str(exc)
        ticket.relay_export_status = "failed"
        return await _move_to_manual(
            session,
            ticket=ticket,
            task_type="sap_export_policy_or_address_conflict",
            reason=str(exc),
        )

    customer_codes = {str(line.customer_code or "").strip() for line in lines}
    if len(customer_codes) != 1 or "" in customer_codes:
        export.status = "manual_review"
        export.error_code = "SAP_BATCH_CUSTOMER_CODE_CONFLICT"
        return await _move_to_manual(
            session,
            ticket=ticket,
            task_type="sap_batch_customer_conflict",
            reason=export.error_code,
        )
    if any(line.status in {"waiting_sap_result", "waiting_rma", "rma_received"} for line in lines):
        return {
            "status": export.status,
            "export_id": export.id,
            "line_count": len(lines),
            "idempotent_reuse": True,
        }

    # SourceRequestIDs must survive a worker crash or unknown external commit.
    await session.flush()
    await session.commit()
    export = await session.get(TicketRelayExport, export_id, with_for_update=True)
    if export is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RELAY_EXPORT_NOT_FOUND")
    ticket = await session.get(RepairTicket, export.ticket_id, with_for_update=True)
    lines = list(
        (
            await session.execute(
                select(ExportSap)
                .where(ExportSap.relay_export_id == export.id)
                .order_by(ExportSap.ticket_item_id)
                .with_for_update()
            )
        ).scalars().all()
    )
    if export.status == "submitting" or any(line.status == "submitting" for line in lines):
        confirm_at = utcnow() + timedelta(seconds=settings.RELAY_SUBMIT_UNKNOWN_CONFIRM_SECONDS)
        export.status = "submit_unknown"
        export.error_code = "SAP_SUBMIT_INTERRUPTED_REQUIRES_RECONCILIATION"
        export.next_retry_at = confirm_at
        ticket.relay_export_status = "submit_unknown"
        for line in lines:
            line.status = "submit_unknown"
            line.last_error_code = export.error_code
            line.next_retry_at = confirm_at
        await session.flush()
        return await reconcile_uncertain_submission(
            session,
            export_id=export.id,
            reason="interrupted_submit_recovery",
            user_id=None,
            schedule_jobs=schedule_jobs,
        )
    export.status = "submitting"
    export.attempt_count += 1
    ticket.relay_export_status = "submitting"
    now = utcnow()
    for line in lines:
        line.status = "submitting"
        line.attempt_count += 1
        line.submitted_at = line.submitted_at or now
        line.last_error_code = None
        line.last_error_message = None
    operation = await start_external_operation(
        session,
        operation_type="relay_insert",
        operation_key=f"relay-export:{export.id}:submit:{export.attempt_count}",
        ticket_id=ticket.id,
        email_id=ticket.source_email_id,
        export_sap_id=lines[0].id,
        recovery_stage="source_request_batch_submit",
        details={
            "source_request_ids": [line.source_request_id for line in lines],
            "sns": [line.sn for line in lines],
        },
    )
    # Persist the in-flight marker and operation evidence before touching SQL
    # Server. A restarted worker must reconcile, never blindly reinsert.
    await session.flush()
    await session.commit()
    adapter = create_sap_middleware_adapter()
    items = [
        ExternalRmaSubmissionItem(
            source_request_id=uuid.UUID(line.source_request_id),
            sn=line.sn,
            payload=_line_payload(line),
        )
        for line in lines
    ]
    try:
        await adapter.submit_rma_batch(items)
    except SapUnknownCommitStateError as exc:
        confirm_at = utcnow() + timedelta(seconds=settings.RELAY_SUBMIT_UNKNOWN_CONFIRM_SECONDS)
        export.status = "submit_unknown"
        export.error_code = "SAP_SUBMIT_RESULT_UNKNOWN"
        export.error_message = str(exc)[:2000]
        export.next_retry_at = confirm_at
        ticket.relay_export_status = "submit_unknown"
        for line in lines:
            line.status = "submit_unknown"
            line.last_error_code = export.error_code
            line.last_error_message = export.error_message
            line.next_retry_at = confirm_at
        fail_external_operation(
            operation,
            error_code=export.error_code,
            error_message=export.error_message,
            retryable=True,
            uncertain=True,
            recovery_stage="source_request_batch_reconcile",
        )
        result = await reconcile_uncertain_submission(
            session,
            export_id=export.id,
            reason="immediate_unknown_commit_check",
            user_id=None,
            schedule_jobs=schedule_jobs,
        )
        if result["status"] == "submit_unknown" and schedule_jobs:
            await notify_ticket_once(
                session,
                ticket=ticket,
                event_type="sap_submit_unknown",
                title="SAP 提交结果等待自动核对",
                content=(
                    f"工单 {ticket.ticket_no} 的整批提交结果未知；系统将在 "
                    f"{settings.RELAY_SUBMIT_UNKNOWN_CONFIRM_SECONDS} 秒后按 SourceRequestID 再次核对。"
                ),
                priority="high",
                metadata={"relay_export_id": export.id},
            )
            poll_job = await enqueue_job(
                session,
                job_type="sap_rma_poll",
                resource_type="ticket_relay_export",
                resource_id=export.id,
                idempotency_key=f"sap_submit_reconcile:{export.id}:{export.attempt_count}",
                metadata={"ticket_id": ticket.id, "reconcile_submit": True},
                max_attempts=5,
            )
            poll_job.next_run_at = confirm_at
            result["reconcile_job_id"] = poll_job.id
        return result
    except Exception as exc:
        export.status = "submit_failed"
        export.error_code = "SAP_BATCH_SUBMIT_FAILED"
        export.error_message = str(exc)[:2000]
        ticket.relay_export_status = "failed"
        for line in lines:
            line.status = "submit_failed"
            line.last_error_code = export.error_code
            line.last_error_message = export.error_message
        fail_external_operation(
            operation,
            error_code=export.error_code,
            error_message=export.error_message,
            retryable=True,
            recovery_stage="source_request_batch_submit",
        )
        await notify_ticket_once(
            session,
            ticket=ticket,
            event_type="sap_export_failed",
            title="SAP 整批提交失败",
            content=f"工单 {ticket.ticket_no} 的 SAP 提交失败，可在工单详情查看原因并安全重试。",
            priority="high",
            metadata={"relay_export_id": export.id, "error_code": export.error_code},
        )
        return {"status": "submit_failed", "error_code": export.error_code, "export_id": export.id}

    accepted_at = utcnow()
    for line in lines:
        line.status = "waiting_sap_result"
        line.accepted_at = accepted_at
        line.next_retry_at = None
    succeed_external_operation(
        operation,
        details={"source_request_ids": [line.source_request_id for line in lines]},
    )
    export.status = "waiting_sap_result"
    export.error_code = None
    export.error_message = None
    export.next_retry_at = None
    export.exported_at = utcnow()
    ticket.relay_export_status = "waiting_rma"
    ticket.rma_status = "waiting_sap"
    await notify_ticket_once(
        session,
        ticket=ticket,
        event_type="sap_export_accepted",
        title="SAP 提交已写入",
        content=f"工单 {ticket.ticket_no} 的 {len(lines)} 个 SN 已作为同一事务写入中转库。",
        metadata={
            "relay_export_id": export.id,
            "line_count": len(lines),
            "source_request_ids": [line.source_request_id for line in lines],
        },
    )
    poll_job = None
    if schedule_jobs:
        poll_job = await enqueue_job(
            session,
            job_type="sap_rma_poll",
            resource_type="ticket_relay_export",
            resource_id=export.id,
            idempotency_key=f"sap_rma_poll:{export.id}:{export.payload_hash[:16]}",
            metadata={"ticket_id": ticket.id, "ticket_version": ticket.version},
            max_attempts=5000,
        )
    return {
        "status": "waiting_sap_result",
        "export_id": export.id,
        "line_count": len(lines),
        "poll_job_id": poll_job.id if poll_job is not None else None,
    }


async def reconcile_uncertain_submission(
    session: AsyncSession,
    *,
    export_id: int,
    reason: str,
    user_id: int | None,
    schedule_jobs: bool = True,
) -> dict[str, Any]:
    export = await session.get(TicketRelayExport, export_id, with_for_update=True)
    if export is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RELAY_EXPORT_NOT_FOUND")
    ticket = await session.get(RepairTicket, export.ticket_id, with_for_update=True)
    if export is None or ticket is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="SAP_EXPORT_CONTEXT_MISSING")
    if export.status not in {"submit_unknown", "manual_review"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SAP_SUBMISSION_RECONCILIATION_NOT_ALLOWED",
        )
    if export.status == "manual_review" and export.error_code != "SAP_SUBMIT_PARTIAL_REMOTE_ROWS":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SAP_SUBMISSION_RECONCILIATION_NOT_ALLOWED",
        )
    lines = list(
        (
            await session.execute(
                select(ExportSap)
                .where(ExportSap.relay_export_id == export.id)
                .order_by(ExportSap.ticket_item_id)
                .with_for_update()
            )
        ).scalars().all()
    )
    if not lines:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="SAP_EXPORT_LINES_MISSING")
    adapter = create_sap_middleware_adapter()
    results = await adapter.find_records_by_source_request_ids(
        [uuid.UUID(line.source_request_id) for line in lines]
    )
    found = {str(row.source_request_id): row for row in results}
    expected = {line.source_request_id for line in lines}
    found_ids = set(found)
    if found_ids == expected:
        now = utcnow()
        for line in lines:
            result = found[line.source_request_id]
            line.status = "rma_received" if result.rma_no else "waiting_sap_result"
            line.accepted_at = line.accepted_at or now
            line.next_retry_at = None
            line.last_error_code = None
            line.last_error_message = None
        export.status = "waiting_sap_result"
        export.error_code = None
        export.error_message = None
        export.next_retry_at = None
        ticket.relay_export_status = "waiting_rma"
        poll_job = None
        if schedule_jobs:
            poll_job = await enqueue_job(
                session,
                job_type="sap_rma_poll",
                resource_type="ticket_relay_export",
                resource_id=export.id,
                idempotency_key=f"sap_rma_poll:{export.id}:{export.payload_hash[:16]}",
                metadata={"ticket_id": ticket.id, "ticket_version": ticket.version},
                max_attempts=5000,
            )
        return {
            "status": "waiting_sap_result",
            "export_id": export.id,
            "found_count": len(found_ids),
            "poll_job_id": poll_job.id if poll_job is not None else None,
        }
    if found_ids:
        export.status = "manual_review"
        export.error_code = "SAP_SUBMIT_PARTIAL_REMOTE_ROWS"
        export.error_message = f"expected={len(expected)},found={len(found_ids)}"
        for line in lines:
            line.status = "manual_review"
            line.last_error_code = export.error_code
        return await _move_to_manual(
            session,
            ticket=ticket,
            task_type="sap_submit_partial_remote_rows",
            reason=export.error_code,
        )
    now = utcnow()
    if export.status == "submit_unknown" and export.next_retry_at and now < export.next_retry_at:
        return {
            "status": "submit_unknown",
            "export_id": export.id,
            "found_count": 0,
            "confirm_after": export.next_retry_at,
        }
    for line in lines:
        line.status = "pending"
        line.next_retry_at = None
        line.last_error_code = None
        line.last_error_message = None
    export.status = "pending"
    export.next_retry_at = None
    export.error_code = None
    export.error_message = None
    ticket.relay_export_status = "pending"
    retry_job = None
    if schedule_jobs:
        retry_job = await enqueue_job(
            session,
            job_type="relay_ticket_export",
            resource_type="ticket_relay_export",
            resource_id=export.id,
            idempotency_key=f"relay_ticket_export_unknown_retry:{export.id}:{export.attempt_count}",
            metadata={"ticket_id": ticket.id, "reason": reason, "user_id": user_id},
            max_attempts=5,
        )
    return {
        "status": "pending",
        "export_id": export.id,
        "ticket_id": ticket.id,
        "retry_job_id": retry_job.id if retry_job is not None else None,
    }


def _working_seconds_between(started_at: datetime, ended_at: datetime) -> float:
    shanghai = ZoneInfo("Asia/Shanghai")
    start = started_at.replace(tzinfo=timezone.utc).astimezone(shanghai)
    end = ended_at.replace(tzinfo=timezone.utc).astimezone(shanghai)
    if end <= start:
        return 0
    total = 0.0
    cursor = start.date()
    while cursor <= end.date():
        if cursor.weekday() < 5:
            window_start = datetime.combine(cursor, time(9, 0), tzinfo=shanghai)
            window_end = datetime.combine(cursor, time(18, 0), tzinfo=shanghai)
            segment_start = max(start, window_start)
            segment_end = min(end, window_end)
            if segment_end > segment_start:
                total += (segment_end - segment_start).total_seconds()
        cursor += timedelta(days=1)
    return total


async def poll_export_batch(
    session: AsyncSession,
    *,
    export_id: int,
    allow_late_result: bool = False,
    confirmed_by_user_id: int | None = None,
    schedule_jobs: bool = True,
    enqueue_rma_job: bool = True,
) -> dict[str, Any]:
    export = await session.get(TicketRelayExport, export_id, with_for_update=True)
    if export is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RELAY_EXPORT_NOT_FOUND")
    ticket = await session.get(RepairTicket, export.ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")
    if allow_late_result and export.error_code != "SAP_RMA_TIMEOUT":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SAP_LATE_RESULT_CONFIRMATION_NOT_ALLOWED",
        )
    lines = (
        await session.execute(
            select(ExportSap)
            .where(ExportSap.relay_export_id == export.id)
            .order_by(ExportSap.ticket_item_id)
            .with_for_update()
        )
    ).scalars().all()
    if not lines:
        export.status = "submit_failed"
        export.error_code = "SAP_EXPORT_LINES_MISSING"
        ticket.relay_export_status = "failed"
        return {"status": "submit_failed", "error_code": export.error_code, "export_id": export.id}
    if export.status == "submit_unknown":
        return await reconcile_uncertain_submission(
            session,
            export_id=export.id,
            reason="scheduled_unknown_commit_confirmation",
            user_id=confirmed_by_user_id,
            schedule_jobs=schedule_jobs,
        )

    now = utcnow()
    waiting = False
    poll_operation = await start_external_operation(
        session,
        operation_type="relay_poll",
        operation_key=f"relay-export:{export.id}:poll:{now.isoformat()}",
        ticket_id=ticket.id,
        email_id=ticket.source_email_id,
        export_sap_id=lines[0].id,
        recovery_stage="source_request_result_poll",
        details={"source_request_ids": [line.source_request_id for line in lines]},
    )
    try:
        results = await create_sap_middleware_adapter().find_records_by_source_request_ids(
            [uuid.UUID(line.source_request_id) for line in lines]
        )
    except Exception as exc:
        fail_external_operation(
            poll_operation,
            error_code="RELAY_RMA_POLL_FAILED",
            error_message=str(exc)[:2000],
            retryable=True,
            recovery_stage="source_request_result_poll",
        )
        raise
    by_source_id = {str(result.source_request_id): result for result in results}
    succeed_external_operation(
        poll_operation,
        details={
            "found_count": len(by_source_id),
            "expected_count": len(lines),
            "rma_numbers": sorted({row.rma_no for row in results if row.rma_no}),
        },
    )
    for line in lines:
        line.last_polled_at = now
        result = by_source_id.get(line.source_request_id)
        if result is None or not result.rma_no:
            line.status = "waiting_rma"
            waiting = True
            continue
        try:
            rma_no = validate_rma_no(result.rma_no)
        except ValueError as exc:
            line.status = "manual_review"
            line.last_error_code = str(exc)
            export.status = "manual_review"
            export.error_code = str(exc)
            ticket.relay_export_status = "failed"
            return await _move_to_manual(
                session,
                ticket=ticket,
                task_type="sap_rma_number_invalid",
                reason=str(exc),
            )
        line.rma_no = rma_no
        line.rma_received_at = now
        line.status = "rma_received"
        line.last_error_code = None
        line.last_error_message = None

    if waiting:
        export.status = "waiting_rma"
        ticket.relay_export_status = "waiting_rma"
        started_at = min((line.submitted_at or export.exported_at or export.created_at) for line in lines)
        timed_out = _working_seconds_between(started_at, now) >= (
            settings.RELAY_SQLSERVER_RMA_TIMEOUT_WORKING_HOURS * 3600
        )
        if timed_out and not allow_late_result:
            for line in lines:
                if line.status == "waiting_rma":
                    line.status = "timed_out"
                    line.last_error_code = "SAP_RMA_TIMEOUT"
            export.status = "manual_review"
            export.error_code = "SAP_RMA_TIMEOUT"
            return await _move_to_manual(
                session,
                ticket=ticket,
                task_type="sap_rma_timeout",
                reason="SAP_RMA_TIMEOUT",
            )
        return {
            "status": "waiting_rma",
            "export_id": export.id,
            "next_poll_seconds": settings.RELAY_SQLSERVER_RMA_POLL_INTERVAL_SECONDS,
        }

    distinct_rmas = sorted({line.rma_no for line in lines if line.rma_no})
    existing_rmas: dict[str, TicketRma | None] = {}
    business_date = ticket.request_date or min(
        (line.repair_requested_at.date() for line in lines if line.repair_requested_at),
        default=now.date(),
    )
    for rma_no in distinct_rmas:
        rows = list(
            (await session.execute(select(TicketRma).where(TicketRma.rma_no == rma_no))).scalars().all()
        )
        existing_rmas[str(rma_no)] = next((row for row in rows if row.ticket_id == ticket.id), None)
        conflicts = [
            row
            for row in rows
            if row.ticket_id != ticket.id
            and (row.customer_code != ticket.customer_code or row.repair_business_date != business_date)
        ]
        if conflicts:
            export.status = "manual_review"
            export.error_code = "RMA_CROSS_TICKET_BUSINESS_IDENTITY_CONFLICT"
            ticket.relay_export_status = "failed"
            for line in lines:
                line.status = "manual_review"
                line.last_error_code = export.error_code
            return await _move_to_manual(
                session,
                ticket=ticket,
                task_type="duplicate_rma_business_identity_conflict",
                reason=export.error_code,
            )

    export.status = "rma_received"
    ticket.relay_export_status = "rma_received"
    if len(distinct_rmas) != 1:
        export.status = "manual_review"
        export.error_code = "MULTIPLE_RMA_NUMBERS_REQUIRE_MANUAL_REVIEW"
        ticket.relay_export_status = "failed"
        return await _move_to_manual(
            session,
            ticket=ticket,
            task_type="multiple_rma_numbers_for_ticket",
            reason="MULTIPLE_RMA_NUMBERS_REQUIRE_MANUAL_REVIEW",
        )
    unresolved_routes = [
        line
        for line in lines
        if (line.policy_snapshot or {}).get("return_route_status") != "resolved"
        or not (line.policy_snapshot or {}).get("shipping_address")
        or not (line.policy_snapshot or {}).get("shipping_contact")
        or not (line.policy_snapshot or {}).get("shipping_phone")
    ]
    if unresolved_routes:
        export.status = "manual_review"
        export.error_code = "RMA_RETURN_ROUTE_UNRESOLVED"
        ticket.rma_status = "manual_review"
        return await _move_to_manual(
            session,
            ticket=ticket,
            task_type="return_route_review",
            reason="RMA_RETURN_ROUTE_UNRESOLVED",
        )
    for rma_no in distinct_rmas:
        rma = existing_rmas[str(rma_no)]
        matching = [line for line in lines if line.rma_no == rma_no]
        if rma is None:
            rma = TicketRma(
                ticket_id=ticket.id,
                rma_no=rma_no or "",
                customer_code=ticket.customer_code,
                repair_business_date=business_date,
                status="received",
                policy_snapshot={"lines": [line.policy_snapshot for line in matching]},
                received_at=now,
            )
            session.add(rma)
            await session.flush()
        elif (
            rma.status == "received"
            and rma.reply_record_id is None
            and rma.pdf_oss_object_id is None
            and rma.sent_at is None
            and rma.issued_at is None
        ):
            # A newer, still-unsent export snapshot may legitimately reuse the
            # same SAP RMA number after policy or return-route correction. Keep
            # the mutable RMA draft aligned with the latest accepted lines;
            # issued/sent RMA evidence remains immutable.
            rma.policy_snapshot = {
                "lines": [line.policy_snapshot for line in matching]
            }
            rma.received_at = now
        for line in (line for line in lines if line.rma_no == rma_no):
            existing_link = await session.scalar(
                select(TicketRmaItem).where(TicketRmaItem.ticket_item_id == line.ticket_item_id)
            )
            if existing_link is None:
                session.add(
                    TicketRmaItem(
                        ticket_rma_id=rma.id,
                        ticket_item_id=line.ticket_item_id,
                    )
                )

    if allow_late_result and ticket.current_status_code == "manual_review":
        timeout_task = await session.scalar(
            select(ManualReviewTask)
            .where(
                ManualReviewTask.ticket_id == ticket.id,
                ManualReviewTask.task_type == "sap_rma_timeout",
                ManualReviewTask.status.in_({"pending", "assigned", "claimed"}),
            )
            .order_by(ManualReviewTask.id.desc())
        )
        if timeout_task is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="SAP_TIMEOUT_MANUAL_TASK_NOT_FOUND",
            )
        timeout_task.status = "resolved"
        timeout_task.resolved_by_user_id = confirmed_by_user_id
        timeout_task.resolved_at = now
        timeout_task.resolution = "迟到的 SAP RMA 回填已由主管确认。"
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="ready_for_export",
            trigger_event="manual_resolved",
            user_id=confirmed_by_user_id,
            operator_type="user",
            reason="主管确认接收迟到的 SAP RMA 回填结果。",
            metadata={
                "safety_check_hash": ticket.safety_check_hash,
                "rma_no": distinct_rmas[0],
                "late_result_confirmed": True,
            },
            resolving_task_id=timeout_task.id,
        )

    ticket.rma_status = "received"
    export.error_code = None
    export.error_message = None
    rma_no = distinct_rmas[0]
    job = None
    if enqueue_rma_job:
        job = await enqueue_job(
            session,
            job_type="rma_authorization",
            resource_type="repair_ticket",
            resource_id=ticket.id,
            idempotency_key=f"rma_authorization:{ticket.id}:{ticket.version}:{rma_no}",
            metadata={
                "ticket_version": ticket.version,
                "safety_check_hash": ticket.safety_check_hash,
                "sn_validation_hash": ticket.sn_validation_hash,
                "rma_no": rma_no,
                "rma_template_version": RMA_TEMPLATE_VERSION,
            },
            max_attempts=1,
        )
    return {
        "status": "rma_received",
        "export_id": export.id,
        "rma_no": rma_no,
        "rma_job_id": job.id if job is not None else None,
    }
