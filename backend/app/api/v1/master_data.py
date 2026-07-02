from __future__ import annotations

from fastapi import APIRouter

from app.core.response import page

router = APIRouter()


@router.get("/sn-assets")
async def list_sn_assets_placeholder() -> dict:
    return page([], total=0)

