from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    BoardCard,
    Email,
    ExportSap,
    ManualReviewTask,
    RepairTicket,
    RepairTicketItem,
    SnAsset,
    TicketRelayExport,
    TicketRma,
    TicketRmaItem,
)
from app.services.common import model_to_dict, utcnow
from app.services.business_notifications import notify_ticket_once
from app.services.customer_policies import resolve_customer_policy
from app.services.external_relay import poll_rma_from_relay, push_ticket_snapshot_to_relay
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
    "submission_key",
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


def _submission_key(ticket_id: int, item_id: int, ticket_version: int, payload_hash: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"repair-mail-agent:sap:{ticket_id}:{item_id}:{ticket_version}:{payload_hash}",
        )
    )


def _is_in_warranty(asset: SnAsset | None, requested_on: date) -> bool:
    if asset is None or asset.warranty_end_date is None:
        return False
    if asset.warranty_start_date and requested_on < asset.warranty_start_date:
        return False
    return requested_on <= asset.warranty_end_date


def _address_details(route: str) -> tuple[str, str, str]:
    if route == "beijing":
        values = (
            settings.RMA_DEFAULT_BEIJING_ADDRESS,
            settings.RMA_DEFAULT_BEIJING_CONTACT,
            settings.RMA_DEFAULT_BEIJING_PHONE,
        )
        error_code = "BEIJING_SHIPPING_ADDRESS_NOT_CONFIGURED"
    else:
        values = (
            settings.RMA_DEFAULT_TIANJIN_ADDRESS,
            settings.RMA_DEFAULT_TIANJIN_CONTACT,
            settings.RMA_DEFAULT_TIANJIN_PHONE,
        )
        error_code = "TIANJIN_SHIPPING_ADDRESS_NOT_CONFIGURED"
    if any(not value.strip() for value in values):
        raise ValueError(error_code)
    return values


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
    prepared: list[tuple[RepairTicketItem, dict[str, Any], str, str, str, str]] = []
    routes: set[str] = set()

    for item in items:
        sn = (item.sn or "").strip().upper()
        material_code = (item.material_code or "").strip()
        if not sn or not ticket.customer_code or not material_code:
            raise ValueError("SAP_EXPORT_REQUIRED_FIELDS_MISSING")
        board = await session.scalar(
            select(BoardCard).where(
                BoardCard.material_code == material_code,
                BoardCard.status == "active",
            )
        )
        route = "beijing" if board is not None else "tianjin"
        routes.add(route)
        asset = await session.get(SnAsset, item.sn_asset_id) if item.sn_asset_id else None
        policy_result = await resolve_customer_policy(
            session,
            customer_code=ticket.customer_code,
            requested_on=requested_on,
            in_warranty=_is_in_warranty(asset, requested_on),
        )
        if policy_result["status"] != "resolved":
            raise ValueError(str(policy_result.get("error_code") or "CUSTOMER_POLICY_UNRESOLVED"))
        policy = dict(policy_result["policy"])
        address, shipping_contact, shipping_phone = _address_details(route)
        policy["shipping_route"] = route
        policy["shipping_address"] = address
        prepared.append((item, policy, address, shipping_contact, shipping_phone, sn))

    if len(routes) > 1:
        raise ValueError("MIXED_SHIPPING_ADDRESS_REQUIRES_MANUAL_REVIEW")

    rows: list[ExportSap] = []
    for item, policy, address, shipping_contact, shipping_phone, sn in prepared:
        payload = {
            "sn": sn,
            "customer_code": ticket.customer_code,
            "material_code": item.material_code,
            "customer_name": ticket.customer_name,
            "material_name": item.material_name,
            "contact_person": shipping_contact,
            "contact_phone": shipping_phone,
            "email_subject": source_email.subject if source_email else None,
            "problem_description": item.failure_description or ticket.problem_description,
            "repair_requested_at": requested_on.isoformat(),
            "mailing_address": address,
            "currency": policy["currency"],
            "shipping_fee": policy["shipping_fee_text"],
            "repair_fee": policy["repair_price"],
            "tax_rate": policy["tax_rate"],
        }
        payload_hash = _stable_hash({"payload": payload, "policy_snapshot": policy})
        row = ExportSap(
            ticket_id=ticket.id,
            ticket_item_id=item.id,
            relay_export_id=export.id,
            ticket_version=ticket.version,
            submission_key=_submission_key(ticket.id, item.id, ticket.version, payload_hash),
            payload_hash=payload_hash,
            policy_snapshot=policy,
            status="pending",
            sn=payload["sn"],
            customer_code=payload["customer_code"],
            material_code=payload["material_code"],
            customer_name=payload["customer_name"],
            material_name=payload["material_name"],
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
        "submission_key": row.submission_key,
        "sn": row.sn,
        "customer_code": row.customer_code,
        "material_code": row.material_code,
        "customer_name": row.customer_name,
        "material_name": row.material_name,
        "contact_person": row.contact_person,
        "contact_phone": row.contact_phone,
        "email_subject": row.email_subject,
        "problem_description": row.problem_description,
        "repair_requested_at": row.repair_requested_at,
        "mailing_address": row.mailing_address,
        "currency": row.currency,
        "shipping_fee": row.shipping_fee,
        "repair_fee": row.repair_fee,
    }


async def submit_export_batch(
    session: AsyncSession,
    *,
    export_id: int,
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

    export.status = "running"
    export.attempt_count += 1
    ticket.relay_export_status = "running"
    for line in lines:
        if line.status in {"accepted", "rma_received"} and line.remote_call_id:
            continue
        line.status = "submitting"
        line.attempt_count += 1
        try:
            result = await push_ticket_snapshot_to_relay(_line_payload(line))
        except Exception as exc:
            line.status = "failed"
            line.last_error_code = "RELAY_TICKET_EXPORT_FAILED"
            line.last_error_message = str(exc)[:2000]
            export.status = "failed"
            export.error_code = line.last_error_code
            export.error_message = line.last_error_message
            ticket.relay_export_status = "failed"
            await notify_ticket_once(
                session,
                ticket=ticket,
                event_type="sap_export_failed",
                title="SAP 提交失败",
                content=f"工单 {ticket.ticket_no} 的 SAP 提交失败，可在工单详情查看原因并重试。",
                priority="high",
                metadata={
                    "relay_export_id": export.id,
                    "line_id": line.id,
                    "error_code": line.last_error_code,
                },
            )
            return {
                "status": "failed",
                "error_code": line.last_error_code,
                "error_message": line.last_error_message,
                "export_id": export.id,
                "line_id": line.id,
            }
        if result.get("status") != "succeeded" or not result.get("remote_record_key"):
            line.status = "failed"
            line.last_error_code = f"RELAY_{str(result.get('status') or 'FAILED').upper()}"
            export.status = "failed"
            export.error_code = line.last_error_code
            ticket.relay_export_status = "failed"
            await notify_ticket_once(
                session,
                ticket=ticket,
                event_type="sap_export_failed",
                title="SAP 提交失败",
                content=f"工单 {ticket.ticket_no} 的 SAP 提交未被中转库受理，可在工单详情重试。",
                priority="high",
                metadata={
                    "relay_export_id": export.id,
                    "line_id": line.id,
                    "error_code": line.last_error_code,
                },
            )
            return {"status": "failed", "error_code": line.last_error_code, "export_id": export.id}
        now = utcnow()
        line.status = "accepted"
        line.remote_call_id = str(result["remote_record_key"])
        line.submitted_at = line.submitted_at or now
        line.accepted_at = now
        line.last_error_code = None
        line.last_error_message = None

    export.status = "accepted"
    export.remote_record_key = ",".join(line.remote_call_id or "" for line in lines)[:191]
    export.error_code = None
    export.error_message = None
    export.exported_at = utcnow()
    ticket.relay_export_status = "accepted"
    ticket.rma_status = "waiting_sap"
    await notify_ticket_once(
        session,
        ticket=ticket,
        event_type="sap_export_accepted",
        title="SAP 提交已受理",
        content=f"工单 {ticket.ticket_no} 的 {len(lines)} 个 SN 已写入中转库并取得 callID。",
        metadata={
            "relay_export_id": export.id,
            "line_count": len(lines),
            "call_ids": [line.remote_call_id for line in lines],
        },
    )
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
        "status": "accepted",
        "export_id": export.id,
        "line_count": len(lines),
        "poll_job_id": poll_job.id,
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
    if not lines or any(not line.remote_call_id for line in lines):
        export.status = "failed"
        export.error_code = "SAP_EXPORT_CALL_ID_MISSING"
        ticket.relay_export_status = "failed"
        return {"status": "failed", "error_code": "SAP_EXPORT_CALL_ID_MISSING", "export_id": export.id}

    now = utcnow()
    waiting = False
    for line in lines:
        if line.status == "rma_received" and line.rma_no:
            continue
        result = await poll_rma_from_relay(line.remote_call_id or "")
        line.last_polled_at = now
        if result.get("status") != "rma_received":
            line.status = "waiting_rma"
            waiting = True
            continue
        try:
            rma_no = validate_rma_no(str(result.get("rma_no") or ""))
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
    for rma_no in distinct_rmas:
        rma = await session.scalar(select(TicketRma).where(TicketRma.rma_no == rma_no))
        if rma is not None and rma.ticket_id != ticket.id:
            export.status = "manual_review"
            export.error_code = "RMA_NUMBER_ALREADY_LINKED_TO_OTHER_TICKET"
            ticket.relay_export_status = "failed"
            for line in lines:
                if line.rma_no == rma_no:
                    line.status = "manual_review"
                    line.last_error_code = export.error_code
            return await _move_to_manual(
                session,
                ticket=ticket,
                task_type="duplicate_rma_number",
                reason=export.error_code,
            )
        if rma is None:
            matching = [line for line in lines if line.rma_no == rma_no]
            rma = TicketRma(
                ticket_id=ticket.id,
                rma_no=rma_no or "",
                status="received",
                policy_snapshot={"lines": [line.policy_snapshot for line in matching]},
                received_at=now,
            )
            session.add(rma)
            await session.flush()
        for line in (line for line in lines if line.rma_no == rma_no):
            existing_link = await session.scalar(
                select(TicketRmaItem).where(TicketRmaItem.ticket_item_id == line.ticket_item_id)
            )
            if existing_link is None:
                session.add(TicketRmaItem(ticket_rma_id=rma.id, ticket_item_id=line.ticket_item_id))

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
        "rma_job_id": job.id,
    }
