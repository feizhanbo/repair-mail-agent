from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok, page
from app.models import JobRunLog
from app.services.jobs import enqueue_job, serialize_job
from app.services.storage import StorageUploadError, generate_presigned_url_for_object


router = APIRouter()
exports_router = APIRouter()


class ExportJobRequest(BaseModel):
    kind: str
    filters: dict = Field(default_factory=dict)
    ids: list[int] = Field(default_factory=list, max_length=1000)


_EXPORT_FILTERS = {
    "emails": {"parse_status", "intent_type", "intent_subtype", "keyword", "subject", "from_address", "message_id", "received_start", "received_end"},
    "tickets": {"status_code", "keyword", "ticket_no", "customer", "contact", "sn", "assigned_user_id", "request_date_start", "request_date_end"},
    "sn_assets": {"keyword", "sn", "customer", "material", "asset_status"},
    "board_cards": {
        "keyword",
        "board_code",
        "board_name",
        "customer_scope",
        "return_location",
        "status",
    },
}


def _can_access_job(current_user: CurrentUser, job: JobRunLog) -> bool:
    if {"admin", "supervisor"} & set(current_user.roles):
        return True
    metadata = job.metadata_json if isinstance(job.metadata_json, dict) else {}
    return metadata.get("user_id") == current_user.id


@exports_router.post("/jobs")
async def create_export_job(
    payload: ExportJobRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    if not ({"admin", "supervisor"} & set(current_user.roles)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")
    allowed = _EXPORT_FILTERS.get(payload.kind)
    if allowed is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EXPORT_KIND_NOT_SUPPORTED")
    if set(payload.filters) - allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EXPORT_FILTER_NOT_SUPPORTED")
    if payload.ids and payload.kind == "emails":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EXPORT_SELECTION_NOT_SUPPORTED")
    from app.core.request_context import get_correlation_id

    job = await enqueue_job(
        session,
        job_type="export_generate",
        resource_type="export",
        resource_id=None,
        idempotency_key=f"export:{get_correlation_id() or 'job'}",
        metadata={"kind": payload.kind, "filters": payload.filters, "ids": payload.ids, "user_id": current_user.id},
    )
    await session.commit()
    return ok(serialize_job(job), "export queued")


@router.get("")
async def list_jobs(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    job_type: str | None = None,
    job_status: str | None = Query(default=None, alias="status"),
) -> dict:
    if not ({"admin", "supervisor"} & set(current_user.roles)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")
    statement = select(JobRunLog)
    if job_type:
        statement = statement.where(JobRunLog.job_type == job_type)
    if job_status:
        statement = statement.where(JobRunLog.status == job_status)
    total = int(await session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (
        await session.execute(
            statement.order_by(JobRunLog.created_at.desc()).offset((page_no - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return page([serialize_job(row) for row in rows], total=total, page_no=page_no, page_size=page_size)


@router.get("/{job_id}")
async def get_job(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    job = await session.get(JobRunLog, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="JOB_NOT_FOUND")
    if not _can_access_job(current_user, job):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")
    return ok(serialize_job(job))


@router.get("/{job_id}/download-url")
async def job_download_url(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    expires_seconds: int = Query(3600, ge=60, le=86400),
) -> dict:
    job = await session.get(JobRunLog, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="JOB_NOT_FOUND")
    if not _can_access_job(current_user, job):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")
    if job.status != "success" or not job.output_oss_object_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="JOB_OUTPUT_NOT_READY")
    try:
        url = await generate_presigned_url_for_object(
            session, oss_object_id=job.output_oss_object_id, expires_seconds=expires_seconds
        )
    except StorageUploadError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="JOB_OUTPUT_NOT_READY") from exc
    return ok({
        "job_id": job.id,
        "object_id": job.output_oss_object_id,
        "url": url,
        "expires_seconds": expires_seconds,
    })
