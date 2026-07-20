from __future__ import annotations

import asyncio
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_roles
from app.core.database import get_session
from app.core.response import ok, page
from app.models import ManualReviewTask, ParseResult, Role, User, UserRole
from app.schemas.business import IdsRequest, ParseResultApplyRequest, TicketExportConfirmRequest, TicketFieldPatchRequest, TicketItemsPatchRequest, TicketOwnerUpdateRequest, TicketTransitionRequest
from app.services.master_data import EXCEL_MEDIA_TYPE, xlsx_bytes
from app.services import tickets as ticket_service
from app.services.email_flow_trace import build_ticket_timeline
from app.services.workflow import transition_ticket
from app.services.ticket_safety import build_safety_report, validate_and_mark_ready_for_export

router = APIRouter()


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
    if payload.to_status_code == "ready_for_export":
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="EXPORT_SAFETY_GATE_REQUIRED")
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
