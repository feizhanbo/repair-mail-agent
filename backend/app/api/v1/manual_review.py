from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok, page
from app.schemas.business import ManualTaskAssignRequest, ManualTaskReparseRequest, ManualTaskResolveRequest
from app.services import manual_review as manual_review_service
from app.workflows.executions import (
    enqueue_manual_resume_if_bound,
    ensure_interrupt_action_allowed,
    get_pending_task_interrupt,
)

router = APIRouter()


@router.get("/tasks")
async def list_tasks(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: str | None = None,
    task_type: str | None = None,
    priority: str | None = None,
    assigned_user_id: int | None = None,
    scope: str | None = Query(None, pattern="^(mine|all)$"),
    category: str | None = Query(None, pattern="^(rma|sql)$"),
    created_start: date | None = None,
    created_end: date | None = None,
) -> dict:
    if scope is None:
        scope = "all"
    items, total = await manual_review_service.list_tasks(
        session,
        page=page_no,
        page_size=page_size,
        task_status=status,
        task_type=task_type,
        priority=priority,
        assigned_user_id=assigned_user_id,
        current_user_id=current_user.id,
        scope=scope,
        category=category,
        created_start=created_start,
        created_end=created_end,
    )
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    del current_user
    return ok(await manual_review_service.get_task_detail(session, task_id))


@router.post("/tasks/{task_id}/claim")
async def claim_task(
    task_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    result = await manual_review_service.claim_task(session, task_id=task_id, user_id=current_user.id)
    await session.commit()
    return ok(result, "manual task claimed")


@router.post("/tasks/{task_id}/assign")
async def assign_task(
    task_id: int,
    payload: ManualTaskAssignRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    result = await manual_review_service.assign_task(
        session,
        task_id=task_id,
        assigned_user_id=payload.assigned_user_id,
        operator_user_id=current_user.id,
        reason=payload.reason,
    )
    await session.commit()
    return ok(result, "manual task assigned")


@router.post("/tasks/{task_id}/release")
async def release_task(
    task_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    result = await manual_review_service.release_task(session, task_id=task_id, user_id=current_user.id)
    await session.commit()
    return ok(result, "manual task released")


@router.post("/tasks/{task_id}/resolve")
async def resolve_task(
    task_id: int,
    payload: ManualTaskResolveRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    action_by_next = {
        "transition_ready_for_export": "validate",
        "reparse": "reparse",
        "generate_followup": "request_customer_info",
        "wait_customer_info": "request_customer_info",
        "finish_external_handling": "close",
        "resolve_manual_business": "close",
    }
    bound_interrupt = await get_pending_task_interrupt(session, manual_task_id=task_id)
    graph_owned = bound_interrupt is not None
    if graph_owned:
        if bound_interrupt[1].status == "resume_queued":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="WORKFLOW_INTERRUPT_RESUME_ALREADY_QUEUED",
            )
        if payload.next_action not in action_by_next:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="WORKFLOW_INTERRUPT_NEXT_ACTION_NOT_SUPPORTED",
            )
        try:
            ensure_interrupt_action_allowed(
                bound_interrupt[1],
                action=action_by_next[payload.next_action],
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        result = await manual_review_service.record_graph_task_decision(
            session,
            task_id=task_id,
            user_id=current_user.id,
            resolution=payload.resolution,
            resolution_type=payload.resolution_type,
            next_action=payload.next_action,
            result_payload=payload.result_payload,
        )
    else:
        result = await manual_review_service.resolve_task(
            session,
            task_id=task_id,
            user_id=current_user.id,
            resolution=payload.resolution,
            resolution_type=payload.resolution_type,
            next_action=payload.next_action,
            result_payload=payload.result_payload,
        )
    resume = None
    resolved_task = result.get("task") if isinstance(result, dict) else None
    if payload.next_action in action_by_next and isinstance(resolved_task, dict) and resolved_task.get("status") == "resolved":
        ticket = result.get("ticket") if isinstance(result, dict) else None
        resume = await enqueue_manual_resume_if_bound(
            session,
            manual_task_id=task_id,
            action=action_by_next[payload.next_action],
            edited_fields=manual_review_service.graph_edited_fields(payload.result_payload),
            reviewer_id=current_user.id,
            expected_ticket_version=ticket.get("version") if isinstance(ticket, dict) else None,
            next_action=payload.next_action,
        )
    if resume is not None:
        result["workflow_resume"] = resume
    await session.commit()
    return ok(result, "manual task resolved")


@router.post("/tasks/{task_id}/reparse")
async def reparse_task(
    task_id: int,
    payload: ManualTaskReparseRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    bound_interrupt = await get_pending_task_interrupt(session, manual_task_id=task_id)
    if bound_interrupt is not None:
        if bound_interrupt[1].status == "resume_queued":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="WORKFLOW_INTERRUPT_RESUME_ALREADY_QUEUED",
            )
        try:
            ensure_interrupt_action_allowed(bound_interrupt[1], action="reparse")
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        result = await manual_review_service.record_graph_task_decision(
            session,
            task_id=task_id,
            user_id=current_user.id,
            resolution=payload.reason or f"manual review task {task_id} requested reparse",
            resolution_type=payload.mode,
            next_action="reparse",
            result_payload={"mode": payload.mode},
        )
        resume = await enqueue_manual_resume_if_bound(
            session,
            manual_task_id=task_id,
            action="reparse",
            edited_fields={},
            reviewer_id=current_user.id,
            expected_ticket_version=bound_interrupt[1].expected_ticket_version,
            next_action="reparse",
        )
        if resume is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="WORKFLOW_INTERRUPT_NOT_FOUND_FOR_TASK",
            )
        result["workflow_resume"] = resume
    else:
        result = await manual_review_service.reparse_task(
            session,
            task_id=task_id,
            user_id=current_user.id,
            mode=payload.mode,
            reason=payload.reason,
        )
    await session.commit()
    return ok(result, "manual task reparsed")
