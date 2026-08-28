from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date
from email.utils import parseaddr
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Email,
    RepairTicket,
    RepairTicketItem,
    SnAsset,
    SnValidationResult,
    TicketRelayExport,
)
from app.services.business_rules import required_missing_for_ticket
from app.services.business_resolution import (
    resolve_and_snapshot_ticket_policy,
    resolve_ticket_return_routes,
)
from app.services.common import utcnow
from app.services.sap_sn_sync import sn_snapshot_freshness
from app.services.external_relay import relay_configured
from app.services.jobs import enqueue_job
from app.services.workflow import create_manual_task_if_missing, transition_ticket


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CJK = re.compile(r"[\u3400-\u9fff]")
_SN_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_+\-]{3,99}$")
_FIELD_LIMITS = {
    "customer_code": 50,
    "customer_name": 255,
    "contact_person": 100,
    "contact_phone": 100,
    "contact_email": 255,
    "mailing_address": 500,
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_customer_name(value: Any) -> str:
    return re.sub(r"[\s（）()·・,，.。]+", "", _clean_text(value)).casefold()


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _valid_email(value: str) -> bool:
    address = parseaddr(value)[1]
    return bool(
        address
        and address == value
        and "@" in address
        and "." in address.rsplit("@", 1)[-1]
        and "\r" not in value
        and "\n" not in value
    )


def _domain(address: str) -> str:
    parsed = parseaddr(address)[1].lower()
    return parsed.rsplit("@", 1)[-1] if "@" in parsed else ""


def _language(text: str) -> str:
    meaningful = re.sub(r"\s+", "", text)
    if not meaningful:
        return "unknown"
    return "zh-CN" if len(_CJK.findall(meaningful)) / max(1, len(meaningful)) >= 0.05 else "en-US"


async def _ticket_and_items(session: AsyncSession, ticket_id: int) -> tuple[RepairTicket, list[RepairTicketItem]]:
    ticket = await session.get(RepairTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")
    items = (
        await session.execute(
            select(RepairTicketItem)
            .where(RepairTicketItem.ticket_id == ticket.id)
            .order_by(RepairTicketItem.line_no, RepairTicketItem.id)
        )
    ).scalars().all()
    return ticket, list(items)


def _sn_input_snapshot(ticket: RepairTicket, items: list[RepairTicketItem]) -> dict[str, Any]:
    return {
        "ticket_id": ticket.id,
        "ticket_version": ticket.version,
        "customer_code": _clean_text(ticket.customer_code),
        "customer_name": _clean_text(ticket.customer_name),
        "items": [
            {
                "id": item.id,
                "line_no": item.line_no,
                "sn": _clean_text(item.sn).upper(),
                "material_code": _clean_text(item.material_code),
                "material_name": _clean_text(item.material_name),
            }
            for item in items
        ],
    }


def _remote_value(record: dict[str, Any], local_field: str) -> Any:
    remote_column = (settings.RELAY_SQLSERVER_SN_COLUMN_MAP or {}).get(local_field)
    return record.get(remote_column) if remote_column else None


async def build_sn_validation_report(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_id: int | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    """Validate SN evidence only. This stage never marks a ticket exportable."""
    ticket, items = await _ticket_and_items(session, ticket_id)
    if persist:
        normalized_sns = [_clean_text(item.sn).upper() for item in items if _clean_text(item.sn)]
        assets = list(
            (
                await session.execute(select(SnAsset).where(SnAsset.sn.in_(normalized_sns)))
            ).scalars().all()
        ) if normalized_sns else []
        customer_codes = {asset.customer_code for asset in assets if asset.customer_code}
        customer_names = {asset.customer_name for asset in assets if asset.customer_name}
        assets_by_sn = {_clean_text(asset.sn).upper(): asset for asset in assets if _clean_text(asset.sn)}
        normalized_names = {_normalized_customer_name(name) for name in customer_names}
        if (
            not ticket.customer_code
            and ticket.customer_name
            and len(customer_codes) == 1
            and _normalized_customer_name(ticket.customer_name) in normalized_names
        ):
            ticket.customer_code = next(iter(customer_codes))
        # SN validation hashes the post-enrichment state. Otherwise filling
        # material data during the same validation immediately makes the
        # freshly stored hash stale when the full safety report recomputes it.
        for item in items:
            asset = assets_by_sn.get(_clean_text(item.sn).upper())
            if asset is None:
                continue
            if not item.material_code:
                item.material_code = asset.material_code
            if not item.material_name:
                item.material_name = asset.material_name
    input_snapshot = _sn_input_snapshot(ticket, items)
    input_hash = _stable_hash(input_snapshot)

    if (
        not persist
        and ticket.sn_validation_status == "passed"
        and ticket.sn_validation_hash == input_hash
        and ticket.sn_validation_snapshot
    ):
        return {
            "passed": True,
            "status": "passed",
            "errors": {},
            "snapshot": ticket.sn_validation_snapshot,
            "input_hash": input_hash,
            "reused": True,
        }

    errors: dict[str, str] = {}
    checks: list[dict[str, Any]] = []
    normalized_sns = [_clean_text(item.sn).upper() for item in items if _clean_text(item.sn)]
    duplicate_sns = {sn for sn, count in Counter(normalized_sns).items() if count > 1}
    if not items:
        errors["items"] = "at_least_one_item_required"
    elif len(items) > 300:
        errors["items"] = "maximum_300_items"

    sqlserver_live = (
        settings.RELAY_SQLSERVER_ENABLED
        and settings.RELAY_ADAPTER.strip().lower() == "sqlserver"
    )
    source_system = "sqlserver_snapshot+local_sn_assets" if sqlserver_live else "local_sn_assets"
    if sqlserver_live:
        freshness = await sn_snapshot_freshness(session)
        if not freshness["fresh"]:
            errors["sn_snapshot"] = f"sqlserver_snapshot_{freshness['status']}"
    for item in items:
        prefix = f"items.{item.line_no}"
        normalized_sn = _clean_text(item.sn).upper()
        check: dict[str, Any] = {
            "item_id": item.id,
            "line_no": item.line_no,
            "sn": normalized_sn,
            "source": source_system,
            "status": "failed",
        }
        asset: SnAsset | None = None
        if not normalized_sn:
            errors[f"{prefix}.sn"] = "required"
        elif not _SN_PATTERN.fullmatch(normalized_sn):
            errors[f"{prefix}.sn"] = "invalid_format"
        elif normalized_sn in duplicate_sns:
            errors[f"{prefix}.sn"] = "duplicate_sn"
        else:
            asset = await session.scalar(select(SnAsset).where(SnAsset.sn == normalized_sn))
            if asset is None:
                errors[f"{prefix}.sn"] = "sn_not_found"
            else:
                check.update(
                    {
                        "asset_id": asset.id,
                        "asset_ins_id": asset.ins_id,
                        "asset_status": asset.asset_status,
                        "asset_customer_code": asset.customer_code,
                        "asset_customer_name": asset.customer_name,
                        "asset_material_code": asset.material_code,
                        "asset_material_name": asset.material_name,
                        "warranty_start_date": asset.warranty_start_date.isoformat() if asset.warranty_start_date else None,
                        "warranty_end_date": asset.warranty_end_date.isoformat() if asset.warranty_end_date else None,
                        "asset_source_system": asset.source_system,
                    }
                )
                if asset.asset_status != "valid":
                    errors[f"{prefix}.sn"] = "sn_not_valid"
                if asset.ins_id is None:
                    errors[f"{prefix}.ins_id"] = "required_from_sn_master"
                elif not isinstance(asset.ins_id, int):
                    errors[f"{prefix}.ins_id"] = "invalid_type"
                if ticket.customer_code and asset.customer_code != ticket.customer_code:
                    errors[f"{prefix}.customer"] = "sn_customer_mismatch"
                if item.material_code and asset.material_code != item.material_code:
                    errors[f"{prefix}.material"] = "sn_material_mismatch"

        item_errors = {key: value for key, value in errors.items() if key.startswith(prefix)}
        passed = not item_errors
        check["status"] = "passed" if passed else "failed"
        check["errors"] = item_errors
        checks.append(check)

        if persist:
            item.sn = normalized_sn or item.sn
            item.validation_status = "pass" if passed else "failed"
            item.validation_message = "SN core safety validation passed" if passed else "; ".join(item_errors.values())[:500]
            if asset is not None:
                item.sn_asset_id = asset.id
                if not item.material_code:
                    item.material_code = asset.material_code
                if not item.material_name:
                    item.material_name = asset.material_name
            session.add(
                SnValidationResult(
                    ticket_id=ticket.id,
                    ticket_item_id=item.id,
                    sn=normalized_sn,
                    matched_sn_asset_id=asset.id if asset else None,
                    check_exists=asset is not None,
                    check_valid=asset.asset_status == "valid" if asset else False,
                    check_customer_match=(not ticket.customer_code or ticket.customer_code == asset.customer_code) if asset else False,
                    check_material_match=(not item.material_code or item.material_code == asset.material_code) if asset else False,
                    need_ship_to_beijing=None,
                    result_status="pass" if passed else "failed",
                    result_message=item.validation_message,
                    checked_by="manual" if user_id else "system",
                    ticket_version=ticket.version,
                    input_hash=input_hash,
                    source_system=source_system,
                    evidence_json=check,
                )
            )

    snapshot = {
        "ticket_id": ticket.id,
        "ticket_version": ticket.version,
        "input": input_snapshot,
        "input_hash": input_hash,
        "source": source_system,
        "checks": checks,
    }
    passed = not errors
    if persist:
        ticket.sn_validation_status = "passed" if passed else "failed"
        ticket.sn_validation_snapshot = snapshot
        ticket.sn_validation_hash = input_hash
        ticket.sn_validated_at = utcnow()
        if passed and ticket.conflict_fields:
            remaining_conflicts = dict(ticket.conflict_fields)
            remaining_conflicts.pop("sn", None)
            ticket.conflict_fields = remaining_conflicts
        ticket.safety_check_snapshot = None
        ticket.safety_check_hash = None
        ticket.safety_checked_at = None
    return {
        "passed": passed,
        "status": "passed" if passed else "failed",
        "errors": errors,
        "snapshot": snapshot,
        "input_hash": input_hash,
        "reused": False,
    }


async def _ensure_sn_failure_task(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    user_id: int | None,
    errors: dict[str, str],
) -> None:
    reason = "; ".join(f"{key}:{value}" for key, value in errors.items())[:500]
    if ticket.current_status_code not in {"manual_review", "closed"}:
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="manual_review",
            trigger_event="manual_review_required",
            user_id=user_id,
            reason=reason,
            manual_task_type="sn_validation_failed",
            manual_task_priority="high",
        )
    elif ticket.current_status_code == "manual_review":
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="sn_validation_failed",
            trigger_reason=reason,
            priority="high",
            assigned_user_id=ticket.assigned_user_id,
        )


async def validate_ticket_sn_core(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_id: int | None = None,
) -> dict[str, Any]:
    ticket = await session.get(RepairTicket, ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")
    # A manual or recovery validation must refresh evidence from SN master
    # data even when ticket fields are unchanged. Warranty dates, asset status,
    # customer ownership, and material mappings can change independently of
    # the ticket input hash.
    ticket.sn_validation_status = "running"
    report = await build_sn_validation_report(session, ticket_id=ticket.id, user_id=user_id, persist=True)
    if not report["passed"]:
        await _ensure_sn_failure_task(session, ticket=ticket, user_id=user_id, errors=report["errors"])
    return {"ticket_id": ticket.id, "status": report["status"], "report": report}


async def _customer_source_email(session: AsyncSession, ticket: RepairTicket) -> Email | None:
    excluded_senders = {
        address
        for address in (
            parseaddr(settings.IMAP_USER)[1].lower(),
            parseaddr(settings.SMTP_USER)[1].lower(),
            *(parseaddr(value)[1].lower() for value in settings.DEVICE_RECEIPT_TRUSTED_SENDERS),
        )
        if address
    }
    if ticket.thread_id:
        emails = (
            await session.execute(
                select(Email)
                .where(Email.thread_id == ticket.thread_id, Email.mail_direction == "inbound")
                .order_by(Email.sent_at.asc(), Email.received_at.asc(), Email.id.asc())
            )
        ).scalars().all()
        for email in emails:
            if parseaddr(email.from_address)[1].lower() not in excluded_senders:
                return email
    if ticket.source_email_id:
        source = await session.get(Email, ticket.source_email_id)
        if source and parseaddr(source.from_address)[1].lower() not in excluded_senders:
            return source
    return None


async def build_safety_report(session: AsyncSession, *, ticket_id: int) -> dict[str, Any]:
    ticket, items = await _ticket_and_items(session, ticket_id)
    input_hash = _stable_hash(_sn_input_snapshot(ticket, items))
    errors: dict[str, str] = {}
    warnings: dict[str, str] = {}
    if ticket.sn_validation_status != "passed":
        errors["sn_validation"] = f"status_{ticket.sn_validation_status}"
    elif not ticket.sn_validation_hash or ticket.sn_validation_hash != input_hash:
        ticket.sn_validation_status = "stale"
        errors["sn_validation"] = "stale"

    source_email = await _customer_source_email(session, ticket)
    for name in required_missing_for_ticket(ticket, items):
        errors[name] = "required"
    for name in ("mailing_address", "contact_person", "contact_phone"):
        if not _clean_text(getattr(ticket, name)):
            errors[name] = "required_for_sap_export"
    if ticket.policy_resolution_status != "resolved":
        errors["customer_policy"] = f"status_{ticket.policy_resolution_status}"
    if ticket.customer_scope not in {"domestic", "overseas"}:
        errors["customer_scope"] = "unresolved"
    if ticket.charge_status not in {"free", "annual_contract", "chargeable"}:
        errors["charge_status"] = "unresolved_or_manual_confirmation"
    for name, limit in _FIELD_LIMITS.items():
        value = _clean_text(getattr(ticket, name))
        if len(value) > limit:
            errors[name] = f"max_length_{limit}"
        if _CONTROL_CHARS.search(value):
            errors[name] = "control_characters_not_allowed"
    if ticket.problem_description and _CONTROL_CHARS.search(ticket.problem_description):
        errors["problem_description"] = "control_characters_not_allowed"

    contact_email = _clean_text(ticket.contact_email).lower()
    if contact_email and not _valid_email(contact_email):
        errors["contact_email"] = "invalid_email"
    if source_email is None:
        errors["source_email"] = "customer_source_email_not_found"
    else:
        source_address = parseaddr(source_email.from_address)[1].lower()
        if contact_email != source_address:
            errors["contact_email"] = "must_match_customer_source_sender"

    item_snapshots: list[dict[str, Any]] = []
    if not items:
        errors["items"] = "at_least_one_item_required"
    elif len(items) > 300:
        errors["items"] = "maximum_300_items"
    for item in items:
        prefix = f"items.{item.line_no}"
        if not item.material_code:
            errors[f"{prefix}.material_code"] = "required"
        if not item.material_name:
            errors[f"{prefix}.material_name"] = "required"
        if not item.failure_description:
            errors[f"{prefix}.failure_description"] = "required"
        if item.quantity != 1:
            errors[f"{prefix}.quantity"] = "one_sn_requires_quantity_one"
        item_snapshots.append(
            {
                "id": item.id,
                "line_no": item.line_no,
                "material_code": item.material_code,
                "material_name": item.material_name,
                "board_code": item.board_code,
                "board_name": item.board_name,
                "return_location": item.return_location,
                "return_address": item.return_address,
                "return_contact": item.return_contact,
                "return_phone": item.return_phone,
                "return_postal_code": item.return_postal_code,
                "return_route_source": item.return_route_source,
                "return_route_status": item.return_route_status,
                "return_route_snapshot": item.return_route_snapshot,
                "sn": _clean_text(item.sn).upper(),
                "quantity": item.quantity,
                "failure_description": item.failure_description,
                "failure_information": item.failure_information,
                "data_info": item.data_info,
                "remarks": item.remarks,
                "accessories": item.accessories,
                "validation_status": item.validation_status,
            }
        )

    origin_intent = source_email.intent_type if source_email else None
    rma_required = origin_intent == "new_repair"
    language_code = _language(
        "\n".join(
            filter(
                None,
                [
                    source_email.subject if source_email else None,
                    source_email.latest_reply_segment if source_email else None,
                    source_email.clean_body if source_email else None,
                ],
            )
        )
    )
    if rma_required and not 1 <= len(items) <= 300:
        errors["rma.items"] = "rma_pdf_requires_1_to_300_items"
    snapshot = {
        "ticket_id": ticket.id,
        "ticket_no": ticket.ticket_no,
        "ticket_version": ticket.version + (0 if ticket.current_status_code == "ready_for_export" else 1),
        "origin_intent": origin_intent,
        "source_email_id": source_email.id if source_email else None,
        "language_code": language_code,
        "rma_required": rma_required,
        "sn_validation_hash": ticket.sn_validation_hash,
        "sn_validation_snapshot": ticket.sn_validation_snapshot,
        "customer_code": ticket.customer_code,
        "customer_name": ticket.customer_name,
        "customer_scope": ticket.customer_scope,
        "charge_status": ticket.charge_status,
        "policy_resolution_status": ticket.policy_resolution_status,
        "policy_snapshot": ticket.policy_snapshot,
        "contact_person": ticket.contact_person,
        "contact_phone": ticket.contact_phone,
        "contact_email": contact_email,
        "request_date": ticket.request_date.isoformat() if isinstance(ticket.request_date, date) else ticket.request_date,
        "mailing_address": ticket.mailing_address,
        "problem_description": ticket.problem_description,
        "accessories": ticket.accessories,
        "items": item_snapshots,
    }
    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "snapshot": snapshot,
        "snapshot_hash": _stable_hash(snapshot),
        "sn_source": (ticket.sn_validation_snapshot or {}).get("source", "local_sn_assets"),
    }


async def validate_and_mark_ready_for_export(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_id: int | None,
    reason: str | None = None,
    resolving_task_id: int | None = None,
) -> dict[str, Any]:
    ticket = await session.get(RepairTicket, ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")

    sn_result = await validate_ticket_sn_core(session, ticket_id=ticket.id, user_id=user_id)
    if sn_result["status"] != "passed":
        return {"ticket_id": ticket.id, "status": "sn_validation_failed", "sn_result": sn_result, "jobs": []}

    policy_result = await resolve_and_snapshot_ticket_policy(
        session,
        ticket=ticket,
        user_id=user_id,
    )
    route_result = await resolve_ticket_return_routes(session, ticket=ticket)
    report = await build_safety_report(session, ticket_id=ticket.id)
    report["policy_result"] = policy_result
    report["return_route_result"] = route_result
    if not report["passed"]:
        ticket.relay_export_status = "not_required"
        ticket.rma_status = "manual_review" if report["snapshot"].get("rma_required") else "not_required"
        failure_reason = "; ".join(f"{key}:{value}" for key, value in report["errors"].items())[:500]
        if ticket.current_status_code != "manual_review":
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="manual_review",
                trigger_event="manual_review_required",
                user_id=user_id,
                reason=failure_reason,
                manual_task_type="export_safety_failed",
                manual_task_priority="high",
            )
        else:
            await create_manual_task_if_missing(
                session,
                ticket=ticket,
                task_type="export_safety_failed",
                trigger_reason=failure_reason,
                priority="high",
                assigned_user_id=ticket.assigned_user_id,
            )
        return {"ticket_id": ticket.id, "status": "safety_failed", "report": report, "jobs": []}

    # A human "enter ready for export" decision is the explicit resolution of
    # the parse-time missing/conflict markers.  The safety report above is the
    # authoritative re-check; retaining those historical markers would let the
    # ticket transition to ready_for_export and then make the RMA PDF worker
    # reject the same ticket as unresolved.
    if resolving_task_id is not None:
        ticket.missing_fields = {}
        ticket.conflict_fields = {}

    if ticket.current_status_code != "ready_for_export":
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="ready_for_export",
            trigger_event="validation_passed" if ticket.current_status_code == "parsed" else "manual_resolved",
            user_id=user_id,
            reason=reason or "SN and all outbound fields passed the export safety gate",
            metadata={
                "safety_check_hash": report["snapshot_hash"],
                "sn_validation_hash": ticket.sn_validation_hash,
                "sn_source": report["sn_source"],
            },
            resolving_task_id=resolving_task_id,
        )
    report["snapshot"]["ticket_version"] = ticket.version
    report["snapshot_hash"] = _stable_hash(report["snapshot"])
    ticket.language_code = report["snapshot"]["language_code"]
    ticket.rma_required = bool(report["snapshot"]["rma_required"])
    ticket.safety_check_snapshot = report["snapshot"]
    ticket.safety_check_hash = report["snapshot_hash"]
    ticket.safety_checked_at = utcnow()
    relay_ready = relay_configured()
    ticket.relay_export_status = "pending" if ticket.rma_required and relay_ready else "not_required"
    ticket.rma_status = (
        "waiting_sap"
        if ticket.rma_required and relay_ready
        else ("manual_review" if ticket.rma_required else "not_required")
    )

    jobs: list[dict[str, Any]] = []
    if ticket.rma_required and relay_ready:
        export = await session.scalar(
            select(TicketRelayExport).where(
                TicketRelayExport.ticket_id == ticket.id,
                TicketRelayExport.ticket_version == ticket.version,
                TicketRelayExport.payload_hash == report["snapshot_hash"],
            )
        )
        if export is None:
            export = TicketRelayExport(
                ticket_id=ticket.id,
                ticket_version=ticket.version,
                payload_hash=report["snapshot_hash"],
                payload_snapshot=report["snapshot"],
                status="pending",
            )
            session.add(export)
            await session.flush()
        relay_job = await enqueue_job(
            session,
            job_type="relay_ticket_export",
            resource_type="ticket_relay_export",
            resource_id=export.id,
            idempotency_key=f"relay_ticket_export:{ticket.id}:{ticket.version}:{report['snapshot_hash'][:16]}",
            metadata={"user_id": user_id, "ticket_id": ticket.id, "ticket_version": ticket.version},
            max_attempts=5,
        )
        jobs.append({"id": relay_job.id, "job_type": relay_job.job_type})
    if ticket.rma_required and not relay_ready:
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="sap_relay_not_configured",
            trigger_reason="SAP_RELAY_NOT_CONFIGURED",
            priority="high",
            assigned_user_id=ticket.assigned_user_id,
        )
    return {"ticket_id": ticket.id, "status": ticket.current_status_code, "report": report, "jobs": jobs}
