from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok
from app.schemas.business import ParseResultApplyRequest
from app.services import tickets as ticket_service

router = APIRouter()


@router.post("/{parse_result_id}/apply")
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
    )
    await session.commit()
    return ok(result, "parse result applied")
