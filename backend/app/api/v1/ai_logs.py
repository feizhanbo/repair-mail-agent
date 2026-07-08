from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import page
from app.models import AiCallLog
from app.services.common import model_to_dict, paginate_scalars

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
    "error_message",
    "log_file_path",
    "log_line_no",
    "log_record_hash",
    "created_at",
)


@router.get("")
async def list_ai_logs(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
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
    return page([model_to_dict(row, AI_LOG_FIELDS) for row in rows], total=total, page_no=page_no, page_size=page_size)
