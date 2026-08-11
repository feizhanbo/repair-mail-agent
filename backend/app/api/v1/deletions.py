from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok
from app.services import deletions as deletion_service


router = APIRouter()


def raise_deletion_http(exc: deletion_service.DeletionError) -> None:
    detail: str | dict = exc.code
    if exc.data:
        detail = {"code": exc.code, "data": exc.data}
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


@router.get("/{operation_log_id}")
async def get_deletion_operation(
    operation_log_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    try:
        return ok(await deletion_service.get_deletion_operation(session, operation_log_id))
    except deletion_service.DeletionError as exc:
        raise_deletion_http(exc)


@router.post("/{operation_log_id}/retry")
async def retry_deletion_operation(
    operation_log_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    try:
        result = await deletion_service.process_oss_deletion_operation(session, operation_log_id)
        await session.commit()
        return ok(result, "OSS deletion retried")
    except deletion_service.DeletionError as exc:
        raise_deletion_http(exc)
