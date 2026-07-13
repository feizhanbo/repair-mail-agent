from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.config import settings
from app.core.database import get_session
from app.core.response import ok
from app.models import ReplyRecord, ReplyTemplate, WorkflowStatus, WorkflowTransition
from app.schemas.business import ReplyTemplateCreateRequest, ReplyTemplateUpdateRequest, SystemConfigUpdateRequest
from app.services.ai import ai_configured
from app.services.common import model_to_dict
from app.services.runtime_config import read_runtime_config, write_runtime_config

router = APIRouter()


def _config_payload() -> dict:
    runtime = read_runtime_config()
    return {
        "auto_send_enabled": runtime["auto_send_enabled"],
        "reply_send_mode": runtime["reply_send_mode"],
        "auto_send_min_confidence": runtime["auto_send_min_confidence"],
        "max_follow_up": runtime["max_follow_up"],
        "confidence_threshold": runtime["confidence_threshold"],
        "environment_note": "测试环境默认生成人工确认草稿；生产环境可切换为自动发送。",
        "integrations": {
            "imap_configured": bool(settings.IMAP_HOST and settings.IMAP_USER and settings.IMAP_PASSWORD),
            "smtp_configured": bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD),
            "oss_configured": bool(settings.OSS_ENDPOINT and settings.OSS_BUCKET and settings.OSS_ACCESS_KEY and settings.OSS_SECRET_KEY),
            "ai_configured": ai_configured(),
            "ai_provider": settings.AI_PROVIDER,
            "ai_model": settings.AI_MODEL,
            "ai_base_url": settings.AI_BASE_URL,
            "ai_prompt_version": settings.AI_PROMPT_VERSION,
            "ai_timeout_seconds": settings.AI_TIMEOUT_SECONDS,
        },
    }


@router.get("/info")
async def system_info(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
) -> dict:
    del current_user
    statuses = (await session.execute(select(WorkflowStatus).order_by(WorkflowStatus.sort_order, WorkflowStatus.id))).scalars().all()
    transitions = (await session.execute(select(WorkflowTransition).where(WorkflowTransition.enabled == True))).scalars().all()  # noqa: E712
    return ok(
        {
            "app": settings.APP_NAME,
            "env": settings.APP_ENV,
            **_config_payload(),
            "workflow_statuses": [
                model_to_dict(
                    status,
                    ("id", "status_code", "status_name", "status_category", "description", "is_terminal", "sort_order", "enabled"),
                )
                for status in statuses
            ],
            "workflow_transitions": [
                model_to_dict(
                    transition,
                    (
                        "id",
                        "from_status_code",
                        "to_status_code",
                        "trigger_event",
                        "condition_desc",
                        "require_manual",
                        "enabled",
                    ),
                )
                for transition in transitions
            ],
        }
    )


@router.get("/config")
async def get_config(current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))]) -> dict:
    del current_user
    return ok(_config_payload())


@router.patch("/config")
async def update_config(
    payload: SystemConfigUpdateRequest,
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
) -> dict:
    del current_user
    values = payload.model_dump(exclude_unset=True)
    write_runtime_config(values)
    return ok(_config_payload(), "system config updated")


def _reply_template_payload(template: ReplyTemplate) -> dict:
    return model_to_dict(
        template,
        (
            "id",
            "template_code",
            "template_name",
            "template_type",
            "language",
            "version",
            "subject_template",
            "body_template",
            "enabled",
            "created_by_user_id",
            "created_at",
            "updated_at",
        ),
    )


@router.get("/reply-templates")
async def list_reply_templates(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
) -> dict:
    del current_user
    rows = (
        await session.execute(select(ReplyTemplate).order_by(ReplyTemplate.template_type, ReplyTemplate.template_code, ReplyTemplate.version))
    ).scalars().all()
    return ok([_reply_template_payload(row) for row in rows])


@router.post("/reply-templates")
async def create_reply_template(
    payload: ReplyTemplateCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
) -> dict:
    values = payload.model_dump()
    exists = await session.scalar(
        select(ReplyTemplate).where(ReplyTemplate.template_code == values["template_code"], ReplyTemplate.version == values["version"])
    )
    if exists is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="REPLY_TEMPLATE_ALREADY_EXISTS")
    template = ReplyTemplate(**values, created_by_user_id=current_user.id)
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return ok(_reply_template_payload(template), "reply template created")


@router.patch("/reply-templates/{template_id}")
async def update_reply_template(
    template_id: int,
    payload: ReplyTemplateUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
) -> dict:
    template = await session.get(ReplyTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="REPLY_TEMPLATE_NOT_FOUND")
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        setattr(template, key, value)
    template.created_by_user_id = template.created_by_user_id or current_user.id
    await session.commit()
    await session.refresh(template)
    return ok(_reply_template_payload(template), "reply template updated")


@router.delete("/reply-templates/{template_id}")
async def delete_reply_template(
    template_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
) -> dict:
    del current_user
    template = await session.get(ReplyTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="REPLY_TEMPLATE_NOT_FOUND")
    references = int(await session.scalar(select(func.count()).select_from(ReplyRecord).where(ReplyRecord.template_id == template.id)) or 0)
    if references:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="REPLY_TEMPLATE_IN_USE")
    payload = _reply_template_payload(template)
    await session.delete(template)
    await session.commit()
    return ok({"deleted": True, "template": payload}, "reply template deleted")
