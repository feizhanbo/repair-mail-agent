from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.core.response import ok, page
from app.schemas.business import BoardCardImportRequest, SnAssetImportRequest
from app.services import master_data as master_data_service

router = APIRouter()


@router.get("/sn-assets")
async def list_sn_assets(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: str | None = None,
    asset_status: str | None = None,
) -> dict:
    del current_user
    items, total = await master_data_service.list_sn_assets(
        session,
        page=page_no,
        page_size=page_size,
        keyword=keyword,
        asset_status=asset_status,
    )
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.post("/sn-assets/import")
async def import_sn_assets(
    payload: SnAssetImportRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await master_data_service.import_sn_assets(
        session,
        items=payload.items,
        source_file_name=payload.source_file_name,
        source_file_hash=payload.source_file_hash,
        user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "sn assets imported")


@router.get("/board-cards")
async def list_board_cards(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: str | None = None,
    status: str | None = None,
) -> dict:
    del current_user
    items, total = await master_data_service.list_board_cards(session, page=page_no, page_size=page_size, keyword=keyword, status=status)
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.post("/board-cards/import")
async def import_board_cards(
    payload: BoardCardImportRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await master_data_service.import_board_cards(
        session,
        items=payload.items,
        source_file_name=payload.source_file_name,
        source_file_hash=payload.source_file_hash,
        user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "board cards imported")
