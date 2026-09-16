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
    result = await manual_review_service.resolve_task(
        session,
        task_id=task_id,
        user_id=current_user.id,
        resolution=payload.resolution,
        resolution_type=payload.resolution_type,
        next_action=payload.next_action,
        result_payload=payload.result_payload,
        target_first_intent=payload.target_first_intent,
    )
    await session.commit()
    return ok(result, "manual task resolved")


@router.post("/tasks/{task_id}/reparse")
async def reparse_task(
    task_id: int,
    payload: ManualTaskReparseRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    result = await manual_review_service.reparse_task(
        session,
        task_id=task_id,
        user_id=current_user.id,
        mode=payload.mode,
        reason=payload.reason,
    )
    await session.commit()
    return ok(result, "manual task reparsed")
