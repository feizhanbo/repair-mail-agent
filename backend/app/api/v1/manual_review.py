from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok, page
from app.schemas.business import ManualTaskAssignRequest, ManualTaskReparseRequest, ManualTaskResolveRequest
from app.services import manual_review as manual_review_service

router = APIRouter()


@router.get("/tasks")
async def list_tasks(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: str | None = None,
    task_type: str | None = None,
    assigned_user_id: int | None = None,
) -> dict:
    del current_user
    items, total = await manual_review_service.list_tasks(
        session,
        page=page_no,
        page_size=page_size,
        task_status=status,
        task_type=task_type,
        assigned_user_id=assigned_user_id,
    )
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    return ok(await manual_review_service.get_task_detail(session, task_id))


@router.post("/tasks/{task_id}/claim")
async def claim_task(
    task_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await manual_review_service.claim_task(session, task_id=task_id, user_id=current_user.id)
    await session.commit()
    return ok(result, "manual task claimed")


@router.post("/tasks/{task_id}/assign")
async def assign_task(
    task_id: int,
    payload: ManualTaskAssignRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await manual_review_service.assign_task(
        session,
        task_id=task_id,
        assigned_user_id=payload.assigned_user_id,
        operator_user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "manual task assigned")


@router.post("/tasks/{task_id}/release")
async def release_task(
    task_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await manual_review_service.release_task(session, task_id=task_id, user_id=current_user.id)
    await session.commit()
    return ok(result, "manual task released")


@router.post("/tasks/{task_id}/resolve")
async def resolve_task(
    task_id: int,
    payload: ManualTaskResolveRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await manual_review_service.resolve_task(
        session,
        task_id=task_id,
        user_id=current_user.id,
        resolution=payload.resolution,
        next_action=payload.next_action,
    )
    await session.commit()
    return ok(result, "manual task resolved")


@router.post("/tasks/{task_id}/reparse")
async def reparse_task(
    task_id: int,
    payload: ManualTaskReparseRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
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
