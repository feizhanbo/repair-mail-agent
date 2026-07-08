from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok, page
from app.schemas.business import BoardCardImportRequest, SnAssetImportRequest
from app.services import master_data as master_data_service

router = APIRouter()


@router.get("/sn-assets")
async def list_sn_assets(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: str | None = None,
    sn: str | None = None,
    customer: str | None = None,
    material: str | None = None,
    asset_status: str | None = None,
) -> dict:
    del current_user
    items, total = await master_data_service.list_sn_assets(
        session,
        page=page_no,
        page_size=page_size,
        keyword=keyword,
        sn=sn,
        customer=customer,
        material=material,
        asset_status=asset_status,
    )
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.get("/sn-assets/template")
async def sn_assets_template(current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))]) -> Response:
    del current_user
    return Response(
        content=master_data_service.sn_assets_template_xlsx(),
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="sn-assets-template.xlsx"'},
    )


@router.get("/sn-assets/export")
async def export_sn_assets(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
    keyword: str | None = None,
    sn: str | None = None,
    customer: str | None = None,
    material: str | None = None,
    asset_status: str | None = None,
) -> Response:
    del current_user
    content = await master_data_service.export_sn_assets(
        session,
        keyword=keyword,
        sn=sn,
        customer=customer,
        material=material,
        asset_status=asset_status,
    )
    return Response(
        content=content,
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="sn-assets-export.xlsx"'},
    )


@router.post("/sn-assets/import")
async def import_sn_assets(
    payload: SnAssetImportRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
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


@router.post("/sn-assets/import-file")
async def import_sn_assets_file(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    file: UploadFile = File(...),
) -> dict:
    content = await file.read()
    items, file_hash = master_data_service.parse_sn_assets_xlsx(content)
    result = await master_data_service.import_sn_assets(
        session,
        items=items,
        source_file_name=file.filename,
        source_file_hash=file_hash,
        user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "sn assets file imported")


@router.get("/board-cards")
async def list_board_cards(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: str | None = None,
    material_code: str | None = None,
    material_name: str | None = None,
    status: str | None = None,
) -> dict:
    del current_user
    items, total = await master_data_service.list_board_cards(
        session,
        page=page_no,
        page_size=page_size,
        keyword=keyword,
        material_code=material_code,
        material_name=material_name,
        status=status,
    )
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.get("/board-cards/template")
async def board_cards_template(current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))]) -> Response:
    del current_user
    return Response(
        content=master_data_service.board_cards_template_xlsx(),
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="board-cards-template.xlsx"'},
    )


@router.get("/board-cards/export")
async def export_board_cards(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("supervisor"))],
    keyword: str | None = None,
    material_code: str | None = None,
    material_name: str | None = None,
    status: str | None = None,
) -> Response:
    del current_user
    content = await master_data_service.export_board_cards(
        session,
        keyword=keyword,
        material_code=material_code,
        material_name=material_name,
        status=status,
    )
    return Response(
        content=content,
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="board-cards-export.xlsx"'},
    )


@router.post("/board-cards/import")
async def import_board_cards(
    payload: BoardCardImportRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
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


@router.post("/board-cards/import-file")
async def import_board_cards_file(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    file: UploadFile = File(...),
) -> dict:
    content = await file.read()
    items, file_hash = master_data_service.parse_board_cards_xlsx(content)
    result = await master_data_service.import_board_cards(
        session,
        items=items,
        source_file_name=file.filename,
        source_file_hash=file_hash,
        user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "board cards file imported")
