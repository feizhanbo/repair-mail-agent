from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_roles
from app.core.database import get_session
from app.core.response import ok, page
from app.schemas.business import ReplyDraftRequest, ReplyRejectRequest, ReplySendReconcileRequest, ReplyUpdateRequest
from app.services import replies as reply_service
from app.services.jobs import enqueue_job, serialize_job

router = APIRouter()


@router.get("")
async def list_replies(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ticket_id: int | None = None,
    review_status: str | None = None,
    send_status: str | None = None,
) -> dict:
    del current_user
    items, total = await reply_service.list_replies(
        session,
        ticket_id=ticket_id,
        review_status=review_status,
        send_status=send_status,
        page=page_no,
        page_size=page_size,
    )
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.post("/{ticket_id}/draft")
async def create_draft(
    ticket_id: int,
    payload: ReplyDraftRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    result = await reply_service.create_reply_draft(
        session,
        ticket_id=ticket_id,
        user_id=current_user.id,
        reply_type=payload.reply_type,
        related_email_id=payload.related_email_id,
        language=payload.language,
        missing_fields=payload.missing_fields,
    )
    await session.commit()
    return ok(result, "reply draft created")


@router.patch("/{reply_id}")
async def update_reply(
    reply_id: int,
    payload: ReplyUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    result = await reply_service.update_reply(session, reply_id=reply_id, user_id=current_user.id, values=payload.model_dump(exclude_unset=True))
    await session.commit()
    return ok(result, "reply updated")


@router.post("/{reply_id}/approve-send")
async def approve_send(
    reply_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    result = await reply_service.approve_reply(session, reply_id=reply_id, user_id=current_user.id)
    await session.commit()
    return ok(result, "reply approved")


@router.post("/{reply_id}/approve-send/jobs")
async def approve_send_job(
    reply_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    reply = await reply_service.approve_reply_for_async(session, reply_id=reply_id, user_id=current_user.id)
    if reply.send_status == "sent":
        await session.commit()
        return ok({"reply": reply_service.serialize_reply(reply), "job": None}, "reply already sent")
    job = await enqueue_job(
        session,
        job_type="smtp_send",
        resource_type="reply_record",
        resource_id=reply.id,
        idempotency_key=f"smtp_send:{reply.id}",
        metadata={"user_id": current_user.id},
    )
    await session.commit()
    return ok({"reply": reply_service.serialize_reply(reply), "job": serialize_job(job)}, "reply send queued")


@router.post("/{reply_id}/reject")
async def reject_reply(
    reply_id: int,
    payload: ReplyRejectRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    result = await reply_service.reject_reply(session, reply_id=reply_id, user_id=current_user.id, reason=payload.reason)
    await session.commit()
    return ok(result, "reply rejected")


@router.post("/{reply_id}/reconcile-send")
async def reconcile_send(
    reply_id: int,
    payload: ReplySendReconcileRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator", "supervisor"))],
) -> dict:
    result = await reply_service.reconcile_uncertain_reply(
        session,
        reply_id=reply_id,
        user_id=current_user.id,
        outcome=payload.outcome,
        reason=payload.reason,
        smtp_message_id=payload.smtp_message_id,
    )
    await session.commit()
    return ok(result, "uncertain reply send result reconciled")
