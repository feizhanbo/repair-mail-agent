from __future__ import annotations

from fastapi import APIRouter

from app.core.response import page

router = APIRouter()


@router.get("/tasks")
async def list_tasks_placeholder() -> dict:
    return page([], total=0)

