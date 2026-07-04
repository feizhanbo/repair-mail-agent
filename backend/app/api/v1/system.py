from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.config import settings
from app.core.database import get_session
from app.core.response import ok
from app.models import WorkflowStatus, WorkflowTransition
from app.services.common import model_to_dict

router = APIRouter()


@router.get("/info")
async def system_info(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    del current_user
    statuses = (await session.execute(select(WorkflowStatus).order_by(WorkflowStatus.sort_order, WorkflowStatus.id))).scalars().all()
    transitions = (await session.execute(select(WorkflowTransition).where(WorkflowTransition.enabled == True))).scalars().all()  # noqa: E712
    return ok(
        {
            "app": settings.APP_NAME,
            "env": settings.APP_ENV,
            "auto_send_enabled": settings.AUTO_SEND_ENABLED,
            "max_follow_up": settings.MAX_FOLLOW_UP,
            "confidence_threshold": settings.CONFIDENCE_THRESHOLD,
            "integrations": {
                "imap_configured": bool(settings.IMAP_HOST and settings.IMAP_USER and settings.IMAP_PASSWORD),
                "smtp_configured": bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD),
                "oss_configured": bool(settings.OSS_ENDPOINT and settings.OSS_BUCKET and settings.OSS_ACCESS_KEY and settings.OSS_SECRET_KEY),
                "ai_configured": bool(settings.AI_PROVIDER and settings.AI_API_KEY),
                "ai_provider": settings.AI_PROVIDER,
                "ai_model": settings.AI_MODEL,
                "ai_base_url": settings.AI_BASE_URL,
                "ai_prompt_version": settings.AI_PROMPT_VERSION,
                "ai_timeout_seconds": settings.AI_TIMEOUT_SECONDS,
            },
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
