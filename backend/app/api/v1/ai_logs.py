from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok, page
from app.models import AiCallLog, Email, MailFetchRecord, RepairTicket
from app.services.common import model_to_dict, paginate_scalars
from app.services.ai import ai_log_availability, ai_log_diagnostics, read_ai_log_detail
from app.services.audit import log_operation

router = APIRouter()
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

AI_LOG_FIELDS = (
    "id",
    "trace_id",
    "email_id",
    "ticket_id",
    "attachment_id",
    "job_run_id",
    "correlation_id",
    "call_type",
    "provider_name",
    "model_name",
    "prompt_version",
    "schema_version",
    "parser_version",
    "structured_output_method",
    "input_summary",
    "output_summary",
    "parsed_key_result",
    "confidence_score",
    "latency_ms",
    "attempt_count",
    "status",
    "error_code",
    "error_message",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "log_file_path",
    "log_line_no",
    "log_record_hash",
    "created_at",
)


def serialize_ai_log(ai_log: AiCallLog) -> dict:
    data = model_to_dict(ai_log, AI_LOG_FIELDS)
    data.update(ai_log_diagnostics(ai_log))
    data["availability"] = ai_log_availability(ai_log)
    return data


def _business_boundary(value: date, *, next_day: bool = False) -> datetime:
    local_date = value + timedelta(days=1) if next_day else value
    local = datetime.combine(local_date, time.min, tzinfo=BUSINESS_TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


@router.get("/email-usage")
async def list_email_token_usage(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
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
    resolved_email_id = func.coalesce(
        AiCallLog.email_id,
        MailFetchRecord.email_id,
        RepairTicket.source_email_id,
    )
    conditions = [resolved_email_id.is_not(None)]
    if email_id:
        conditions.append(resolved_email_id == email_id)
    if call_type:
        conditions.append(AiCallLog.call_type == call_type)
    if status:
        conditions.append(AiCallLog.status == status)
    if provider_name:
        conditions.append(AiCallLog.provider_name == provider_name)
    if model_name:
        conditions.append(AiCallLog.model_name == model_name)
    if prompt_version:
        conditions.append(AiCallLog.prompt_version == prompt_version)
    if created_start:
        conditions.append(AiCallLog.created_at >= _business_boundary(created_start))
    if created_end:
        conditions.append(AiCallLog.created_at < _business_boundary(created_end, next_day=True))

    joins = (
        AiCallLog.__table__
        .outerjoin(MailFetchRecord, MailFetchRecord.id == AiCallLog.mail_fetch_record_id)
        .outerjoin(RepairTicket, RepairTicket.id == AiCallLog.ticket_id)
        .outerjoin(Email, Email.id == resolved_email_id)
    )
    metered = (
        (AiCallLog.total_tokens.is_not(None))
        | (AiCallLog.input_tokens.is_not(None))
        | (AiCallLog.output_tokens.is_not(None))
    )
    total_expr = func.coalesce(
        AiCallLog.total_tokens,
        func.coalesce(AiCallLog.input_tokens, 0) + func.coalesce(AiCallLog.output_tokens, 0),
    )
    statement = (
        select(
            resolved_email_id.label("email_id"),
            Email.subject,
            Email.from_address,
            func.count(AiCallLog.id).label("call_count"),
            func.sum(case((metered, 1), else_=0)).label("metered_call_count"),
            func.sum(case((~metered, 1), else_=0)).label("unmetered_call_count"),
            func.coalesce(func.sum(AiCallLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(AiCallLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(total_expr), 0).label("total_tokens"),
            func.min(AiCallLog.created_at).label("first_called_at"),
            func.max(AiCallLog.created_at).label("last_called_at"),
        )
        .select_from(joins)
        .where(*conditions)
        .group_by(resolved_email_id, Email.subject, Email.from_address)
        .order_by(func.max(AiCallLog.created_at).desc(), resolved_email_id.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(statement)).mappings().all()
    grouped = (
        select(resolved_email_id.label("email_id"))
        .select_from(joins)
        .where(*conditions)
        .group_by(resolved_email_id)
        .subquery()
    )
    total = int(await session.scalar(select(func.count()).select_from(grouped)) or 0)
    return page([dict(row) for row in rows], total=total, page_no=page_no, page_size=page_size)


@router.get("/{ai_log_id}/detail")
async def get_ai_log_detail(
    ai_log_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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
        statement = statement.where(AiCallLog.created_at >= _business_boundary(created_start))
    if created_end:
        statement = statement.where(AiCallLog.created_at < _business_boundary(created_end, next_day=True))
    statement = statement.order_by(AiCallLog.created_at.desc(), AiCallLog.id.desc())
    rows, total = await paginate_scalars(session, statement, page_no, page_size)
    return page([serialize_ai_log(row) for row in rows], total=total, page_no=page_no, page_size=page_size)
