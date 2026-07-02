from __future__ import annotations

from fastapi import APIRouter

from app.config import settings
from app.core.response import ok

router = APIRouter()


@router.get("/info")
async def system_info_placeholder() -> dict:
    return ok({"app": settings.APP_NAME, "env": settings.APP_ENV, "auto_send_enabled": settings.AUTO_SEND_ENABLED})

