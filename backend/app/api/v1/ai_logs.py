from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok, page
from app.models import AiCallLog
from app.services.common import model_to_dict, paginate_scalars
from app.services.ai import ai_log_diagnostics, read_ai_log_detail
from app.services.audit import log_operation

router = APIRouter()

AI_LOG_FIELDS = (
    "id",
    "trace_id",
    "email_id",
    "ticket_id",
    "call_type",
    "provider_name",
    "model_name",
    "prompt_version",
    "input_summary",
    "output_summary",
    "parsed_key_result",
    "confidence_score",
    "latency_ms",
    "status",
    "error_code",
    "error_message",
    "log_file_path",
    "log_line_no",
    "log_record_hash",
    "created_at",
)


def serialize_ai_log(ai_log: AiCallLog) -> dict:
    data = model_to_dict(ai_log, AI_LOG_FIELDS)
    data.update(ai_log_diagnostics(ai_log))
    return data


@router.get("/{ai_log_id}/detail")
async def get_ai_log_detail(
    ai_log_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    ai_log = await session.get(AiCallLog, ai_log_id)
    if ai_log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI_LOG_NOT_FOUND")
    try:
        detail = await read_ai_log_detail(ai_log)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="AI_LOG_DETAIL_EXPIRED") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await log_operation(
        session,
        user_id=current_user.id,
        operation_type="ai_log_detail_viewed",
        target_type="ai_call_log",
        target_id=ai_log.id,
        email_id=ai_log.email_id,
        ticket_id=ai_log.ticket_id,
        after_data={"trace_id": ai_log.trace_id, "record_hash": ai_log.log_record_hash},
    )
    await session.commit()
    detail["diagnostics"] = ai_log_diagnostics(ai_log)
    return ok(detail)


@router.get("")
async def list_ai_logs(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ticket_id: int | None = None,
    email_id: int | None = None,
    call_type: str | None = None,
    status: str | None = None,
    provider_name: str | None = None,
    model_name: str | None = None,
    prompt_version: str | None = None,
    created_start: date | None = None,
    created_end: date | None = None,
) -> dict:
    del current_user
    statement = select(AiCallLog)
    if ticket_id:
        statement = statement.where(AiCallLog.ticket_id == ticket_id)
    if email_id:
        statement = statement.where(AiCallLog.email_id == email_id)
    if call_type:
        statement = statement.where(AiCallLog.call_type == call_type)
    if status:
        statement = statement.where(AiCallLog.status == status)
    if provider_name:
        statement = statement.where(AiCallLog.provider_name == provider_name)
    if model_name:
        statement = statement.where(AiCallLog.model_name == model_name)
    if prompt_version:
        statement = statement.where(AiCallLog.prompt_version == prompt_version)
    if created_start:
        statement = statement.where(AiCallLog.created_at >= created_start)
    if created_end:
        statement = statement.where(AiCallLog.created_at <= created_end)
    statement = statement.order_by(AiCallLog.created_at.desc(), AiCallLog.id.desc())
    rows, total = await paginate_scalars(session, statement, page_no, page_size)
    return page([serialize_ai_log(row) for row in rows], total=total, page_no=page_no, page_size=page_size)
