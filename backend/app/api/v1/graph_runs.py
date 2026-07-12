from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok, page
from app.schemas.graph import GraphRunStartRequest
from app.services.graph_run_service import start_email_repair_graph, get_graph_run_status
from app.repositories.graph_repository import list_graph_runs

router = APIRouter()


@router.post("/email-repair/start")
async def start_email_repair(
    payload: GraphRunStartRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await start_email_repair_graph(
        session,
        email_id=payload.email_id,
        user_id=current_user.id,
        reason=payload.reason,
    )
    await session.commit()
    return ok(result, "graph run started")


@router.get("")
async def list_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict:
    del current_user
    items, total = await list_graph_runs(session, page=page_no, page_size=page_size, status=status_filter)
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.get("/{graph_run_id}")
async def get_run(
    graph_run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    result = await get_graph_run_status(session, graph_run_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GRAPH_RUN_NOT_FOUND")
    return ok(result)
