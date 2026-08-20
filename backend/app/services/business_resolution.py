from __future__ import annotations

import re
from datetime import date
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BoardCard,
    FieldAuditLog,
    ManualReviewTask,
    RepairTicket,
    RepairTicketItem,
    SnAsset,
)
from app.core.repair_items import normalize_board_code, normalize_board_name
from app.services.audit import log_operation
from app.services.common import to_plain, utcnow
from app.services.customer_policies import resolve_customer_policy
from app.services.workflow import OPEN_TASK_STATUSES, create_manual_task_if_missing


def _normalized_name(value: str | None) -> str:
    return re.sub(r"[\s（）()·・,，.。]+", "", str(value or "")).casefold()


def _in_warranty(asset: SnAsset, requested_on: date) -> bool:
    if asset.warranty_start_date and requested_on < asset.warranty_start_date:
        return False
    return not asset.warranty_end_date or requested_on <= asset.warranty_end_date


def _mark_policy_needs_manual(
    ticket: RepairTicket,
    snapshot: dict[str, Any],
) -> None:
    ticket.customer_scope = None
    ticket.customer_scope_source = None
    ticket.charge_status = "manual_confirmation"
    ticket.charge_status_source = "policy_unresolved"
    ticket.service_policy_id = None
    ticket.policy_resolution_status = "needs_manual"
    ticket.policy_snapshot = snapshot


async def resolve_and_snapshot_ticket_policy(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    user_id: int | None = None,
) -> dict[str, Any]:
    items = list(
        (
            await session.execute(
                select(RepairTicketItem)
                .where(RepairTicketItem.ticket_id == ticket.id)
                .order_by(RepairTicketItem.line_no, RepairTicketItem.id)
            )
        ).scalars()
    )
    assets: list[SnAsset] = []
    errors: list[str] = []
    for item in items:
        asset = (
            await session.get(SnAsset, item.sn_asset_id)
            if item.sn_asset_id is not None
            else None
        )
        if asset is None and item.sn:
            asset = await session.scalar(
                select(SnAsset).where(SnAsset.sn == item.sn.strip().upper())
            )
        if asset is None:
            errors.append(f"SN_ASSET_MISSING:{item.line_no}")
        else:
            assets.append(asset)
    customer_codes = {asset.customer_code.strip().upper() for asset in assets}
    customer_names = {_normalized_name(asset.customer_name) for asset in assets}
    if not items or len(assets) != len(items):
        errors.append("ALL_ITEMS_REQUIRE_VALID_SN_ASSETS")
    if len(customer_codes) != 1:
        errors.append("SN_CUSTOMER_CODE_CONFLICT")
    if len(customer_names) != 1:
        errors.append("SN_CUSTOMER_NAME_CONFLICT")
    parsed_name = _normalized_name(ticket.customer_name)
    if not parsed_name:
        errors.append("CUSTOMER_NAME_REQUIRED_FOR_AUTO_CONFIRMATION")
    elif customer_names and parsed_name not in customer_names:
        errors.append("CUSTOMER_NAME_MISMATCH")
    if errors:
        _mark_policy_needs_manual(
            ticket,
            {"status": "needs_manual", "errors": sorted(set(errors))},
        )
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="customer_policy_review",
            trigger_reason="; ".join(sorted(set(errors)))[:500],
            priority="high",
            assigned_user_id=ticket.assigned_user_id,
        )
        return {"status": "needs_manual", "errors": sorted(set(errors))}

    customer_code = next(iter(customer_codes))
    ticket.customer_code = customer_code
    requested_on = ticket.request_date or utcnow().date()
    warranty_flags = {_in_warranty(asset, requested_on) for asset in assets}
    if len(warranty_flags) > 1:
        _mark_policy_needs_manual(
            ticket,
            {
                "status": "needs_manual",
                "errors": ["MIXED_WARRANTY_STATUS"],
            },
        )
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="customer_policy_review",
            trigger_reason="MIXED_WARRANTY_STATUS",
            priority="high",
            assigned_user_id=ticket.assigned_user_id,
        )
        return {"status": "needs_manual", "errors": ["MIXED_WARRANTY_STATUS"]}

    policy_result = await resolve_customer_policy(
        session,
        customer_code=customer_code,
        requested_on=requested_on,
        in_warranty=True in warranty_flags,
    )
    if policy_result["status"] != "resolved":
        _mark_policy_needs_manual(ticket, policy_result)
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="customer_policy_review",
            trigger_reason=str(policy_result.get("error_code") or "CUSTOMER_POLICY_UNRESOLVED"),
            priority="high",
            assigned_user_id=ticket.assigned_user_id,
        )
        return policy_result

    snapshot = dict(policy_result["policy"])
    ticket.customer_scope = snapshot["customer_scope"]
    ticket.customer_scope_source = "customer_policy"
    ticket.charge_status = snapshot["charge_status"]
    ticket.charge_status_source = snapshot["source"]
    ticket.service_policy_id = snapshot.get("policy_id")
    ticket.policy_resolution_status = "resolved"
    ticket.policy_snapshot = snapshot
    has_special_rma_rules = bool(
        str(snapshot.get("policy_type") or "") == "special_out_of_warranty"
        or str(snapshot.get("currency") or "RMB").upper() not in {"RMB", "CNY"}
        or str(snapshot.get("reply_salutation") or "").strip()
        or snapshot.get("hide_company_name")
        or snapshot.get("force_manual_review")
    )
    if not has_special_rma_rules:
        stale_tasks = (
            await session.execute(
                select(ManualReviewTask).where(
                    ManualReviewTask.ticket_id == ticket.id,
                    ManualReviewTask.task_type == "rma_special_policy_review",
                    ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
                )
            )
        ).scalars().all()
        for task in stale_tasks:
            task.status = "resolved"
            task.resolved_by_user_id = user_id
            task.resolved_at = utcnow()
            task.resolution = "Customer policy was recalculated without special RMA rules."
            await log_operation(
                session,
                user_id=user_id,
                operation_type="manual_task_resolved",
                target_type="manual_review_task",
                target_id=task.id,
                description=task.resolution,
                after_data={
                    "resolution_type": "policy_recalculated",
                    "next_action": "revalidate_export_snapshot",
                },
            )
    return {"status": "resolved", "policy": snapshot}


def _route_payload(
    row: BoardCard,
    *,
    route_source: str,
    board_code: str | None,
    board_name: str | None,
) -> dict[str, Any]:
    return {
        "status": "resolved",
        "route_source": route_source,
        "matched_board_card_id": row.id,
        "board_code": board_code,
        "board_name": board_name,
        "return_location": row.return_location,
        "return_address": row.shipping_address,
        "return_contact": row.shipping_contact,
        "return_phone": row.shipping_phone,
        "return_postal_code": row.postal_code,
        "message": None,
        "evidence": {
            "board_card_id": row.id,
            "customer_scope": row.customer_scope,
            "route_type": row.route_type,
            "source_file_name": row.source_file_name,
            "source_row_no": row.source_row_no,
        },
    }


def _route_complete(row: BoardCard) -> bool:
    return bool(
        row.return_location
        and row.shipping_address
        and row.shipping_contact
        and row.shipping_phone
    )


async def resolve_item_return_route(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    item: RepairTicketItem,
    persist: bool = True,
) -> dict[str, Any]:
    scope = ticket.customer_scope
    board_code = normalize_board_code(item.board_code) or None
    board_name = normalize_board_name(item.board_name) or None
    row: BoardCard | None = None
    source: str | None = None
    message: str | None = None

    if (
        item.return_route_source == "manual_selected"
        and item.return_route_status == "resolved"
        and item.matched_board_card_id
    ):
        selected = await session.get(BoardCard, item.matched_board_card_id)
        if (
            selected is not None
            and selected.status == "active"
            and _route_complete(selected)
            and selected.return_location == item.return_location
        ):
            return _route_payload(
                selected,
                route_source="manual_selected",
                board_code=board_code,
                board_name=board_name,
            )

    if scope == "overseas":
        matches = list(
            (
                await session.execute(
                    select(BoardCard).where(
                        BoardCard.customer_scope == "overseas",
                        BoardCard.route_type == "scope_default",
                        BoardCard.status == "active",
                    )
                )
            ).scalars()
        )
        if len(matches) == 1:
            row, source = matches[0], "overseas_default"
        else:
            message = "OVERSEAS_DEFAULT_ROUTE_MISSING_OR_DUPLICATED"
    elif scope == "domestic":
        material_code = str(item.material_code or "").strip()
        if material_code:
            material_matches = list(
                (
                    await session.execute(
                        select(BoardCard).where(
                            BoardCard.customer_scope == "domestic",
                            BoardCard.route_type == "board_rule",
                            BoardCard.status == "active",
                            BoardCard.material_code == material_code,
                        )
                    )
                ).scalars()
            )
            if len(material_matches) == 1:
                row, source = material_matches[0], "domestic_material_match"
            elif len(material_matches) > 1:
                message = "MATERIAL_CODE_ROUTE_DUPLICATED"
        if row is None and message is None and board_code:
            matches = list(
                (
                    await session.execute(
                        select(BoardCard).where(
                            BoardCard.customer_scope == "domestic",
                            BoardCard.route_type == "board_rule",
                            BoardCard.status == "active",
                            BoardCard.board_code == board_code,
                        )
                    )
                ).scalars()
            )
            exact = [
                candidate
                for candidate in matches
                if board_name
                and _normalized_name(candidate.board_name) == _normalized_name(board_name)
            ]
            locations = {candidate.return_location for candidate in matches}
            address_keys = {
                (
                    candidate.return_location,
                    candidate.shipping_address,
                    candidate.shipping_contact,
                    candidate.shipping_phone,
                    candidate.postal_code,
                )
                for candidate in matches
            }
            if len(exact) == 1:
                row = exact[0]
            elif len(exact) > 1:
                message = "BOARD_EXACT_MATCH_DUPLICATED"
            elif len(locations) == 1 and len(address_keys) == 1 and matches:
                row = matches[0]
            elif len(locations) == 1 and len(address_keys) > 1:
                message = "BOARD_CODE_ADDRESS_CONFLICT"
            elif len(locations) > 1:
                message = "BOARD_CODE_ROUTE_CONFLICT"
            else:
                message = "BOARD_CODE_NOT_FOUND"
            if row is not None:
                source = "domestic_board_match"
        elif row is None and message is None and board_name:
            matches = list(
                (
                    await session.execute(
                        select(BoardCard).where(
                            BoardCard.customer_scope == "domestic",
                            BoardCard.route_type == "board_rule",
                            BoardCard.status == "active",
                            BoardCard.board_name == board_name,
                        )
                    )
                ).scalars()
            )
            message = (
                "BOARD_NAME_ONLY_REQUIRES_MANUAL_CONFIRMATION"
                if len(matches) == 1
                else "BOARD_NAME_NOT_UNIQUE_OR_NOT_FOUND"
            )
        elif row is None and message is None:
            message = "BOARD_INFORMATION_REQUIRED"
    else:
        message = "CUSTOMER_SCOPE_UNRESOLVED"

    if row is not None and not _route_complete(row):
        message = "RETURN_ROUTE_CONTACT_DETAILS_INCOMPLETE"
        row = None

    result = (
        _route_payload(
            row,
            route_source=source or "domestic_board_match",
            board_code=board_code,
            board_name=board_name,
        )
        if row is not None
        else {
            "status": "needs_manual",
            "route_source": None,
            "matched_board_card_id": None,
            "board_code": board_code,
            "board_name": board_name,
            "return_location": None,
            "return_address": None,
            "return_contact": None,
            "return_phone": None,
            "return_postal_code": None,
            "message": message,
            "evidence": {"customer_scope": scope},
        }
    )
    if persist:
        item.matched_board_card_id = result["matched_board_card_id"]
        item.return_location = result["return_location"]
        item.return_address = result["return_address"]
        item.return_contact = result["return_contact"]
        item.return_phone = result["return_phone"]
        item.return_postal_code = result["return_postal_code"]
        item.return_route_source = result["route_source"]
        item.return_route_status = result["status"]
        item.return_route_message = result["message"]
        item.return_route_snapshot = result
    return result


async def resolve_ticket_return_routes(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
) -> dict[str, Any]:
    items = list(
        (
            await session.execute(
                select(RepairTicketItem)
                .where(RepairTicketItem.ticket_id == ticket.id)
                .order_by(RepairTicketItem.line_no, RepairTicketItem.id)
            )
        ).scalars()
    )
    results = [
        await resolve_item_return_route(session, ticket=ticket, item=item)
        for item in items
    ]
    failed = [result for result in results if result["status"] != "resolved"]
    if failed:
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type="return_route_review",
            trigger_reason="; ".join(
                sorted({str(result.get("message")) for result in failed})
            )[:500],
            priority="high",
            assigned_user_id=ticket.assigned_user_id,
        )
    else:
        route_tasks = (
            await session.execute(
                select(ManualReviewTask).where(
                    ManualReviewTask.ticket_id == ticket.id,
                    ManualReviewTask.task_type == "return_route_review",
                    ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
                )
            )
        ).scalars().all()
        for task in route_tasks:
            task.status = "resolved"
            task.resolved_at = utcnow()
            task.resolution = "All ticket item return routes are resolved."
            await log_operation(
                session,
                operation_type="manual_task_resolved",
                target_type="manual_review_task",
                target_id=task.id,
                description=task.resolution,
                after_data={
                    "resolution_type": "return_routes_revalidated",
                    "next_action": "continue_current_business_stage",
                },
            )
    return {
        "status": "resolved" if not failed and results else "needs_manual",
        "items": results,
    }


async def manually_select_item_route(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    item: RepairTicketItem,
    return_location: str,
    user_id: int,
    reason: str,
) -> dict[str, Any]:
    candidates = list(
        (
            await session.execute(
                select(BoardCard).where(
                    BoardCard.return_location == return_location,
                    BoardCard.status == "active",
                    BoardCard.shipping_address.is_not(None),
                    BoardCard.shipping_contact.is_not(None),
                    BoardCard.shipping_phone.is_not(None),
                )
            )
        ).scalars()
    )
    address_keys = {
        (
            row.shipping_address,
            row.shipping_contact,
            row.shipping_phone,
            row.postal_code,
        )
        for row in candidates
    }
    if not candidates or len(address_keys) != 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="RETURN_LOCATION_ADDRESS_NOT_UNIQUE",
        )
    row = candidates[0]
    result = _route_payload(
        row,
        route_source="manual_selected",
        board_code=item.board_code,
        board_name=item.board_name,
    )
    for field, value in {
        "matched_board_card_id": result["matched_board_card_id"],
        "return_location": result["return_location"],
        "return_address": result["return_address"],
        "return_contact": result["return_contact"],
        "return_phone": result["return_phone"],
        "return_postal_code": result["return_postal_code"],
        "return_route_source": result["route_source"],
        "return_route_status": result["status"],
        "return_route_message": None,
        "return_route_snapshot": result,
    }.items():
        old_value = getattr(item, field)
        if to_plain(old_value) == to_plain(value):
            continue
        setattr(item, field, value)
        session.add(
            FieldAuditLog(
                ticket_id=ticket.id,
                ticket_item_id=item.id,
                field_name=field,
                old_value=None if old_value is None else str(to_plain(old_value))[:1000],
                new_value=None if value is None else str(to_plain(value))[:1000],
                source_type="manual",
                reason=reason,
                operator_user_id=user_id,
            )
        )
    await log_operation(
        session,
        user_id=user_id,
        operation_type="ticket_return_route_manually_selected",
        target_type="repair_ticket_item",
        target_id=item.id,
        description=reason,
        after_data=result,
    )
    await session.flush()
    unresolved_route_id = await session.scalar(
        select(RepairTicketItem.id)
        .where(
            RepairTicketItem.ticket_id == ticket.id,
            RepairTicketItem.return_route_status != "resolved",
        )
        .limit(1)
    )
    if unresolved_route_id is None:
        route_tasks = (
            await session.execute(
                select(ManualReviewTask).where(
                    ManualReviewTask.ticket_id == ticket.id,
                    ManualReviewTask.task_type == "return_route_review",
                    ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
                )
            )
        ).scalars().all()
        for task in route_tasks:
            task.status = "resolved"
            task.resolved_by_user_id = user_id
            task.resolved_at = utcnow()
            task.resolution = reason
            await log_operation(
                session,
                user_id=user_id,
                operation_type="manual_task_resolved",
                target_type="manual_review_task",
                target_id=task.id,
                description=reason,
                after_data={
                    "resolution_type": "return_route_manually_selected",
                    "next_action": "revalidate_export_snapshot",
                },
            )
    return result


async def override_ticket_policy(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    charge_status: str,
    customer_scope: str | None,
    user_id: int,
    reason: str,
) -> dict[str, Any]:
    changes = {
        "charge_status": charge_status,
        "charge_status_source": "manual_override",
        "policy_resolution_status": (
            "resolved" if charge_status != "manual_confirmation" else "needs_manual"
        ),
    }
    if customer_scope is not None:
        changes["customer_scope"] = customer_scope
        changes["customer_scope_source"] = "manual_override"
    for field, value in changes.items():
        old_value = getattr(ticket, field)
        if to_plain(old_value) == to_plain(value):
            continue
        setattr(ticket, field, value)
        session.add(
            FieldAuditLog(
                ticket_id=ticket.id,
                field_name=field,
                old_value=None if old_value is None else str(to_plain(old_value))[:1000],
                new_value=None if value is None else str(to_plain(value))[:1000],
                source_type="manual",
                reason=reason,
                operator_user_id=user_id,
            )
        )
    snapshot = dict(ticket.policy_snapshot or {})
    snapshot.update(
        {
            "charge_status": charge_status,
            "customer_scope": ticket.customer_scope,
            "source": "manual_override",
            "override_reason": reason,
            "overridden_by_user_id": user_id,
            "overridden_at": utcnow().isoformat(),
        }
    )
    ticket.policy_snapshot = snapshot
    await log_operation(
        session,
        user_id=user_id,
        operation_type="ticket_policy_overridden",
        target_type="repair_ticket",
        target_id=ticket.id,
        description=reason,
        after_data=snapshot,
    )
    return {
        "status": ticket.policy_resolution_status,
        "policy": snapshot,
    }
