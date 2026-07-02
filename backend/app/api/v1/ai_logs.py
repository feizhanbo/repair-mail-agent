from __future__ import annotations

from fastapi import APIRouter

from app.core.response import page

router = APIRouter()


@router.get("")
async def list_ai_logs_placeholder() -> dict:
    return page([], total=0)

