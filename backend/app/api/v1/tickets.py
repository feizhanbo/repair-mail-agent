from __future__ import annotations

import asyncio
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_roles
from app.core.database import get_session
from app.core.response import ok, page
from app.models import (
    ExportSap,
    ManualReviewTask,
    ParseResult,
    RepairTicketItem,
    ReplyRecord,
    Role,
    TicketRelayExport,
    TicketRma,
    User,
    UserRole,
)
from app.schemas.business import (
    DeviceReceivedConfirmRequest,
    IdsRequest,
    ParseResultApplyRequest,
    RmaManualPolicyApprovalRequest,
    SapSubmitReconcileRequest,
    TicketExportConfirmRequest,
    TicketFieldPatchRequest,
    TicketItemsPatchRequest,
    TicketOwnerUpdateRequest,
    TicketPolicyOverrideRequest,
    TicketReturnRouteManualRequest,
    TicketTransitionRequest,
)
from app.services.audit import log_operation
from app.services.common import utcnow
from app.services.device_receipts import confirm_device_received
from app.services.master_data import EXCEL_MEDIA_TYPE, xlsx_bytes
from app.services import tickets as ticket_service
from app.services.business_resolution import (
    manually_select_item_route,
    override_ticket_policy,
    resolve_and_snapshot_ticket_policy,
    resolve_ticket_return_routes,
)
from app.services.email_flow_trace import build_ticket_timeline
from app.services.workflow import transition_ticket
from app.services.ticket_safety import build_safety_report, validate_and_mark_ready_for_export
from app.services.jobs import enqueue_job, serialize_job
from app.services.rma_pdf import TEMPLATE_VERSION as RMA_TEMPLATE_VERSION
from app.services.sap_rma import poll_export_batch, reconcile_uncertain_submission

router = APIRouter()


def _ensure_policy_route_mutable(ticket: object) -> None:
    if (
        getattr(ticket, "rma_status", None) == "sent"
        or getattr(ticket, "current_status_code", None) == "closed"
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="TERMINAL_TICKET_POLICY_AND_ROUTE_IMMUTABLE",
        )


@router.get("")
async def list_tickets(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_code: str | None = None,
    keyword: str | None = None,
    ticket_no: str | None = None,
    customer: str | None = None,
    contact: str | None = None,
    sn: str | None = None,
    assigned_user_id: int | None = None,
    request_date_start: date | None = None,
    request_date_end: date | None = None,
) -> dict:
    del current_user
    items, total = await ticket_service.list_tickets(
        session,
        page=page_no,
        page_size=page_size,
        status_code=status_code,
        keyword=keyword,
        ticket_no=ticket_no,
        customer=customer,
        contact=contact,
        sn=sn,
        assigned_user_id=assigned_user_id,
        request_date_start=request_date_start,
        request_date_end=request_date_end,
    )
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.get("/export")
async def export_tickets(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    status_code: str | None = None,
    keyword: str | None = None,
    ticket_no: str | None = None,
    customer: str | None = None,
    contact: str | None = None,
    sn: str | None = None,
    assigned_user_id: int | None = None,
    request_date_start: date | None = None,
    request_date_end: date | None = None,
) -> Response:
    del current_user
    rows = await ticket_service.export_tickets(
        session,
        status_code=status_code,
        keyword=keyword,
        ticket_no=ticket_no,
        customer=customer,
        contact=contact,
        sn=sn,
        assigned_user_id=assigned_user_id,
        request_date_start=request_date_start,
        request_date_end=request_date_end,
    )
    fieldnames = [
        "ticket_no",
        "current_status_code",
        "customer_code",
        "customer_name",
        "contact_person",
        "contact_phone",
        "contact_email",
        "request_date",
        "assigned_user_id",
        "followup_count",
        "confidence_score",
        "missing_fields_json",
        "conflict_fields_json",
        "attachment_summary",
        "sn_validation_summary",
        "reply_status_summary",
        "created_at",
        "updated_at",
    ]
    content = await asyncio.to_thread(xlsx_bytes, rows, fieldnames)
    return Response(
        content=content,
        media_type=EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="tickets-export.xlsx"'},
    )


@router.post("/export-selected")
async def export_selected_tickets(
    payload: IdsRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> Response:
    del current_user
    content = await ticket_service.export_tickets_selected(session, ids=payload.ids)
    return Response(
        content=content,
        media_type=EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="tickets-selected-export.xlsx"'},
    )


@router.get("/{ticket_id}/timeline")
async def ticket_timeline(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await build_ticket_timeline(session, ticket_id))


@router.get("/{ticket_id}")
async def get_ticket(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await ticket_service.get_ticket_detail(session, ticket_id))


@router.patch("/{ticket_id}/fields")
async def patch_ticket_fields(
    ticket_id: int,
    payload: TicketFieldPatchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await ticket_service.patch_ticket_fields(
        session,
        ticket_id=ticket_id,
        fields=payload.fields,
        user_id=current_user.id,
        reason=payload.reason,
        version=payload.version,
    )
    await session.commit()
    return ok(result, "ticket fields updated")


@router.patch("/{ticket_id}/items")
async def patch_ticket_items(
    ticket_id: int,
    payload: TicketItemsPatchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await ticket_service.upsert_ticket_items(session, ticket_id=ticket_id, items=payload.items, user_id=current_user.id, reason=payload.reason)
    await session.commit()
    return ok(result, "ticket items updated")


@router.post("/{ticket_id}/transition")
async def transition(
    ticket_id: int,
    payload: TicketTransitionRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    if payload.to_status_code in {"ready_for_export", "rma_sent", "closed"}:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SYSTEM_MANAGED_STATUS_TRANSITION_REQUIRED",
        )
    ticket = await ticket_service.get_ticket(session, ticket_id)
    await transition_ticket(
        session,
        ticket=ticket,
        to_status_code=payload.to_status_code,
        trigger_event=payload.trigger_event,
        user_id=current_user.id,
        reason=payload.reason,
        metadata=payload.metadata,
    )
    await session.commit()
    return ok(await ticket_service.get_ticket_detail(session, ticket_id), "ticket transitioned")


@router.get("/{ticket_id}/export-safety")
async def export_safety(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    del current_user
    return ok(await build_safety_report(session, ticket_id=ticket_id))


@router.post("/{ticket_id}/validate-export")
async def validate_export(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    result = await validate_and_mark_ready_for_export(session, ticket_id=ticket_id, user_id=current_user.id)
    await session.commit()
    return ok(result, "ticket passed export safety gate")


@router.post("/{ticket_id}/validate-sn")
async def validate_sn(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await ticket_service.validate_ticket_sn(session, ticket_id=ticket_id, user_id=current_user.id)
    await session.commit()
    return ok(result, "ticket sn validated")


@router.post("/{ticket_id}/policy/resolve")
async def resolve_ticket_policy(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[
        CurrentUser, Depends(require_roles("operator", "supervisor"))
    ],
) -> dict:
    ticket = await ticket_service.get_ticket(session, ticket_id)
    _ensure_policy_route_mutable(ticket)
    result = await resolve_and_snapshot_ticket_policy(
        session,
        ticket=ticket,
        user_id=current_user.id,
    )
    ticket.version += 1
    await ticket_service._invalidate_export_snapshot(
        session,
        ticket=ticket,
        user_id=current_user.id,
        reason="customer policy recalculated",
        invalidate_sn=False,
    )
    await session.commit()
    return ok(result, "ticket policy resolved")


@router.post("/{ticket_id}/policy/manual-override")
async def manually_override_ticket_policy(
    ticket_id: int,
    payload: TicketPolicyOverrideRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[
        CurrentUser, Depends(require_roles("operator", "supervisor"))
    ],
) -> dict:
    ticket = await ticket_service.get_ticket(session, ticket_id)
    _ensure_policy_route_mutable(ticket)
    result = await override_ticket_policy(
        session,
        ticket=ticket,
        charge_status=payload.charge_status,
        customer_scope=payload.customer_scope,
        user_id=current_user.id,
        reason=payload.reason,
    )
    ticket.version += 1
    await ticket_service._invalidate_export_snapshot(
        session,
        ticket=ticket,
        user_id=current_user.id,
        reason="ticket policy manually overridden",
        invalidate_sn=False,
    )
    await session.commit()
    return ok(result, "ticket policy overridden")


@router.post("/{ticket_id}/return-routes/resolve")
async def resolve_return_routes(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[
        CurrentUser, Depends(require_roles("operator", "supervisor"))
    ],
) -> dict:
    ticket = await ticket_service.get_ticket(session, ticket_id)
    _ensure_policy_route_mutable(ticket)
    result = await resolve_ticket_return_routes(session, ticket=ticket)
    ticket.version += 1
    await ticket_service._invalidate_export_snapshot(
        session,
        ticket=ticket,
        user_id=current_user.id,
        reason="return routes recalculated",
        invalidate_sn=False,
    )
    await session.commit()
    return ok(result, "return routes resolved")


@router.post("/{ticket_id}/items/{item_id}/return-route/manual")
async def manually_select_return_route(
    ticket_id: int,
    item_id: int,
    payload: TicketReturnRouteManualRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[
        CurrentUser, Depends(require_roles("operator", "supervisor"))
    ],
) -> dict:
    ticket = await ticket_service.get_ticket(session, ticket_id)
    _ensure_policy_route_mutable(ticket)
    item = await session.get(RepairTicketItem, item_id)
    if item is None or item.ticket_id != ticket.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="TICKET_ITEM_NOT_FOUND",
        )
    result = await manually_select_item_route(
        session,
        ticket=ticket,
        item=item,
        return_location=payload.return_location,
        user_id=current_user.id,
        reason=payload.reason,
    )
    ticket.version += 1
    await ticket_service._invalidate_export_snapshot(
        session,
        ticket=ticket,
        user_id=current_user.id,
        reason="return route manually selected",
        invalidate_sn=False,
    )
    await session.commit()
    return ok(result, "return route manually selected")


@router.post("/{ticket_id}/sap-export/retry")
async def retry_sap_export(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    ticket = await ticket_service.get_ticket(session, ticket_id)
    export = await session.scalar(
        select(TicketRelayExport)
        .where(TicketRelayExport.ticket_id == ticket.id)
        .order_by(TicketRelayExport.created_at.desc())
    )
    if export is None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RELAY_EXPORT_NOT_FOUND")
    job = await enqueue_job(
        session,
        job_type="relay_ticket_export",
        resource_type="ticket_relay_export",
        resource_id=export.id,
        idempotency_key=f"relay_ticket_export_retry:{export.id}:{export.attempt_count + 1}",
        metadata={"user_id": current_user.id, "ticket_id": ticket.id, "retry": True},
        max_attempts=5,
    )
    await session.commit()
    return ok(serialize_job(job), "SAP export retry queued")


@router.post("/{ticket_id}/sap-export/poll")
async def poll_sap_export(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    del current_user
    ticket = await ticket_service.get_ticket(session, ticket_id)
    export = await session.scalar(
        select(TicketRelayExport)
        .where(TicketRelayExport.ticket_id == ticket.id)
        .order_by(TicketRelayExport.created_at.desc())
    )
    if export is None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RELAY_EXPORT_NOT_FOUND")
    result = await poll_export_batch(session, export_id=export.id)
    await session.commit()
    return ok(result, "SAP RMA status polled")


@router.post("/{ticket_id}/sap-export/confirm-late")
async def confirm_late_sap_result(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
) -> dict:
    ticket = await ticket_service.get_ticket(session, ticket_id)
    export = await session.scalar(
        select(TicketRelayExport)
        .where(TicketRelayExport.ticket_id == ticket.id)
        .order_by(TicketRelayExport.created_at.desc())
    )
    if export is None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RELAY_EXPORT_NOT_FOUND")
    result = await poll_export_batch(
        session,
        export_id=export.id,
        allow_late_result=True,
        confirmed_by_user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "late SAP result confirmed")


@router.post("/{ticket_id}/sap-export/lines/{line_id}/reconcile")
async def reconcile_sap_submission(
    ticket_id: int,
    line_id: int,
    payload: SapSubmitReconcileRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
) -> dict:
    line = await session.get(ExportSap, line_id)
    if line is None or line.ticket_id != ticket_id:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SAP_EXPORT_LINE_NOT_FOUND")
    result = await reconcile_uncertain_submission(
        session,
        line_id=line_id,
        outcome=payload.outcome,
        call_id=payload.call_id,
        reason=payload.reason,
        user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "uncertain SAP submission reconciled")


@router.post("/{ticket_id}/rma/retry-send")
async def retry_rma_send(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    ticket = await ticket_service.get_ticket(session, ticket_id)
    rma_rows = list(
        (
            await session.execute(select(TicketRma).where(TicketRma.ticket_id == ticket.id))
        ).scalars().all()
    )
    if len(rma_rows) != 1:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="RMA_NUMBER_NOT_UNIQUE_FOR_TICKET")
    sent_reply = await session.scalar(
        select(ReplyRecord)
        .where(
            ReplyRecord.ticket_id == ticket.id,
            ReplyRecord.reply_type == "rma_authorization",
            ReplyRecord.send_status == "sent",
        )
        .order_by(ReplyRecord.id.desc())
    )
    if ticket.current_status_code in {"rma_sent", "closed"} and sent_reply is not None:
        from app.services.replies import resolve_completed_rma_tasks, retry_rma_archive

        ticket.rma_status = "sent"
        rma_rows[0].status = (
            "issued" if ticket.current_status_code == "closed" else "sent"
        )
        rma_rows[0].reply_record_id = sent_reply.id
        rma_rows[0].sent_at = sent_reply.sent_at
        await resolve_completed_rma_tasks(
            session,
            ticket_id=ticket.id,
            user_id=current_user.id,
        )
        result = await retry_rma_archive(
            session,
            reply_id=sent_reply.id,
            user_id=current_user.id,
        )
        await session.commit()
        return ok(result, "RMA reply already sent; archive finalization retried")
    reply_count = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(ReplyRecord)
                .where(
                    ReplyRecord.ticket_id == ticket.id,
                    ReplyRecord.reply_type == "rma_authorization",
                )
            )
        )
        or 0
    )
    job = await enqueue_job(
        session,
        job_type="rma_authorization",
        resource_type="repair_ticket",
        resource_id=ticket.id,
        idempotency_key=f"rma_authorization_retry:{ticket.id}:{ticket.version}:{rma_rows[0].rma_no}:{reply_count}",
        metadata={
            "user_id": current_user.id,
            "ticket_version": ticket.version,
            "safety_check_hash": ticket.safety_check_hash,
            "sn_validation_hash": ticket.sn_validation_hash,
            "rma_no": rma_rows[0].rma_no,
            "rma_template_version": RMA_TEMPLATE_VERSION,
        },
        max_attempts=1,
    )
    await session.commit()
    return ok(serialize_job(job), "RMA send retry queued")


@router.post("/{ticket_id}/rma/manual-policy-approve")
async def approve_rma_manual_policy(
    ticket_id: int,
    payload: RmaManualPolicyApprovalRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[
        CurrentUser,
        Depends(require_roles("operator", "supervisor")),
    ],
) -> dict:
    """Approve a special policy for controlled draft generation, never auto-send."""
    ticket = await ticket_service.get_ticket(session, ticket_id)
    if ticket.current_status_code != "ready_for_export":
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="RMA_MANUAL_POLICY_TICKET_NOT_READY",
        )
    rma_rows = list(
        (
            await session.execute(
                select(TicketRma).where(TicketRma.ticket_id == ticket.id)
            )
        ).scalars().all()
    )
    if len(rma_rows) != 1:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="RMA_NUMBER_NOT_UNIQUE_FOR_TICKET",
        )
    rma_record = rma_rows[0]
    snapshot = dict(rma_record.policy_snapshot or {})
    lines = [
        dict(line)
        for line in list(snapshot.get("lines") or [])
        if isinstance(line, dict)
    ]
    if not lines:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="RMA_POLICY_SNAPSHOT_REQUIRED",
        )
    if any(bool(line.get("hide_company_name")) for line in lines):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="RMA_ANONYMOUS_PDF_REQUIRES_SEPARATE_MANUAL_ARTIFACT",
        )
    approved_at = utcnow().isoformat()
    for line in lines:
        line["manual_approved"] = True
        line["manual_approval"] = {
            "approved_by_user_id": current_user.id,
            "approved_at": approved_at,
            "reason": payload.reason,
            "auto_send_allowed": False,
        }
    snapshot["lines"] = lines
    snapshot["manual_send_only"] = True
    rma_record.policy_snapshot = snapshot
    await log_operation(
        session,
        user_id=current_user.id,
        operation_type="rma_special_policy_approved",
        target_type="ticket_rma",
        target_id=rma_record.id,
        ticket_id=ticket.id,
        description=payload.reason,
        after_data={
            "rma_no": rma_record.rma_no,
            "manual_send_only": True,
        },
    )
    job = await enqueue_job(
        session,
        job_type="rma_authorization",
        resource_type="repair_ticket",
        resource_id=ticket.id,
        idempotency_key=(
            f"rma_manual_policy:{ticket.id}:{ticket.version}:"
            f"{rma_record.rma_no}"
        ),
        metadata={
            "user_id": current_user.id,
            "ticket_version": ticket.version,
            "safety_check_hash": ticket.safety_check_hash,
            "sn_validation_hash": ticket.sn_validation_hash,
            "rma_no": rma_record.rma_no,
            "rma_template_version": RMA_TEMPLATE_VERSION,
            "manual_send_only": True,
        },
        max_attempts=1,
    )
    await session.commit()
    return ok(
        serialize_job(job),
        "special policy approved; RMA draft generation queued for manual review",
    )


@router.post("/{ticket_id}/confirm-device-received")
async def confirm_ticket_device_received(
    ticket_id: int,
    payload: DeviceReceivedConfirmRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    result = await confirm_device_received(
        session,
        ticket_id=ticket_id,
        user_id=current_user.id,
        source="manual",
        note=payload.note,
        idempotency_key=payload.idempotency_key,
    )
    await session.commit()
    return ok(result, "company device receipt recorded")


@router.post("/{ticket_id}/confirm-export", deprecated=True)
async def confirm_export(
    ticket_id: int,
    payload: TicketExportConfirmRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
) -> dict:
    del ticket_id, payload, session, current_user
    from fastapi import HTTPException, status
    raise HTTPException(status_code=status.HTTP_410_GONE, detail="EXPORT_CONFIRM_NO_LONGER_CLOSES_TICKET")


@router.patch("/{ticket_id}/owner")
async def correct_owner(
    ticket_id: int,
    payload: TicketOwnerUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    ticket = await ticket_service.get_ticket(session, ticket_id)
    owner = await session.scalar(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            User.id == payload.owner_user_id,
            User.status == "active",
            Role.role_code == "operator",
        )
    )
    if owner is None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OWNER_MUST_BE_ACTIVE_OPERATOR")
    before = ticket.assigned_user_id
    ticket.assigned_user_id = owner.id
    open_tasks = (
        await session.execute(
            select(ManualReviewTask).where(
                ManualReviewTask.ticket_id == ticket.id,
                ManualReviewTask.status.in_(("pending", "assigned", "claimed", "assignment_failed")),
            )
        )
    ).scalars().all()
    from app.services.audit import create_notification, log_operation
    from app.services.notifications import resolve_notifications_for_target

    for task in open_tasks:
        await resolve_notifications_for_target(session, target_type="manual_review_task", target_id=task.id)
        task.assigned_user_id = owner.id
        task.claimed_by_user_id = None
        task.claimed_at = None
        task.status = "pending"
        await create_notification(
            session,
            event_type="manual_review_owner_corrected",
            target_type="manual_review_task",
            target_id=task.id,
            title="人工复核任务负责人已纠正",
            content=f"工单 {ticket.ticket_no} 已由管理员指定给你处理。",
            priority=task.priority,
            recipient_user_id=owner.id,
            recipient_role_code=None,
            metadata={"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "task_type": task.task_type},
        )
    await log_operation(
        session,
        user_id=current_user.id,
        operation_type="ticket_owner_corrected",
        target_type="repair_ticket",
        target_id=ticket.id,
        description=payload.reason,
        before_data={"owner_user_id": before},
        after_data={"owner_user_id": owner.id, "synchronized_open_tasks": len(open_tasks)},
    )
    await session.commit()
    return ok(await ticket_service.get_ticket_detail(session, ticket_id), "ticket owner corrected")


@router.get("/{ticket_id}/parse-results")
async def list_parse_results(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    await ticket_service.get_ticket(session, ticket_id)
    rows = (await session.execute(select(ParseResult).where(ParseResult.ticket_id == ticket_id).order_by(ParseResult.created_at.desc()))).scalars().all()
    return ok([ticket_service.serialize_parse_result(row) for row in rows])


@router.post("/parse-results/{parse_result_id}/apply")
async def apply_parse_result(
    parse_result_id: int,
    payload: ParseResultApplyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await ticket_service.apply_parse_result(
        session,
        parse_result_id=parse_result_id,
        user_id=current_user.id,
        reason=payload.reason,
        action=payload.action,
        selected_fields=payload.selected_fields,
        selected_item_indices=payload.selected_item_indices,
    )
    await session.commit()
    return ok(result, "parse result applied")


@router.get("/{ticket_id}/email-timeline")
async def email_timeline(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await ticket_service.get_ticket_email_timeline(session, ticket_id))


@router.get("/{ticket_id}/attachments")
async def attachments(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await ticket_service.get_ticket_attachments(session, ticket_id))


@router.get("/{ticket_id}/field-evidence")
async def field_evidence(
    ticket_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await ticket_service.get_ticket_field_evidence(session, ticket_id))
