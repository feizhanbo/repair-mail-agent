from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok, page
from app.schemas.business import (
    BoardCardImportRequest,
    CustomerServicePolicyCreateRequest,
    CustomerServicePolicyUpdateRequest,
    IdsRequest,
    SnAssetImportRequest,
)
from app.services import customer_policies
from app.services import master_data as master_data_service
from app.services.jobs import enqueue_job, serialize_job
from app.services.storage import StorageConfigurationError, StorageUploadError, upload_bytes_to_oss

router = APIRouter()


@router.get("/customer-policies")
async def list_customer_policies(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: str | None = None,
    customer_code: str | None = None,
    policy_type: str | None = None,
    enabled: bool | None = None,
) -> dict:
    del current_user
    items, total = await customer_policies.list_policies(
        session,
        page=page_no,
        page_size=page_size,
        keyword=keyword,
        customer_code=customer_code,
        policy_type=policy_type,
        enabled=enabled,
    )
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.post("/customer-policies")
async def create_customer_policy(
    payload: CustomerServicePolicyCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    result = await customer_policies.create_policy(
        session,
        values=payload.model_dump(),
        user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "customer policy created")


@router.patch("/customer-policies/{policy_id}")
async def update_customer_policy(
    policy_id: int,
    payload: CustomerServicePolicyUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    result = await customer_policies.update_policy(
        session,
        policy_id=policy_id,
        values=payload.model_dump(exclude_unset=True),
    )
    await session.commit()
    return ok(result, "customer policy updated")


@router.get("/sn-assets")
async def list_sn_assets(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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
async def sn_assets_template(current_user: Annotated[CurrentUser, Depends(require_roles("operator"))]) -> Response:
    del current_user
    return Response(
        content=await asyncio.to_thread(master_data_service.sn_assets_template_xlsx),
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="sn-assets-template.xlsx"'},
    )


@router.get("/sn-assets/export")
async def export_sn_assets(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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


@router.post("/sn-assets/export-selected")
async def export_selected_sn_assets(
    payload: IdsRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> Response:
    del current_user
    content = await master_data_service.export_sn_assets_selected(session, ids=payload.ids)
    return Response(
        content=content,
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="sn-assets-selected-export.xlsx"'},
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
    items, file_hash = await asyncio.to_thread(master_data_service.parse_sn_assets_xlsx, content)
    result = await master_data_service.import_sn_assets(
        session,
        items=items,
        source_file_name=file.filename,
        source_file_hash=file_hash,
        user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "sn assets file imported")


@router.post("/sn-assets/import-file/jobs")
async def import_sn_assets_file_job(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    file: UploadFile = File(...),
) -> dict:
    return await _queue_master_data_file(session, current_user, file, kind="sn_assets")


@router.get("/board-cards")
async def list_board_cards(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: str | None = None,
    board_code: str | None = None,
    board_name: str | None = None,
    customer_scope: str | None = None,
    return_location: str | None = None,
    # Deprecated aliases kept for one compatibility release.
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
        board_code=board_code or material_code,
        board_name=board_name or material_name,
        customer_scope=customer_scope,
        return_location=return_location,
        status=status,
    )
    return page(items, total=total, page_no=page_no, page_size=page_size)


@router.get("/board-cards/template")
async def board_cards_template(current_user: Annotated[CurrentUser, Depends(require_roles("operator"))]) -> Response:
    del current_user
    return Response(
        content=await asyncio.to_thread(master_data_service.board_cards_template_xlsx),
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="board-cards-template.xlsx"'},
    )


@router.get("/board-cards/export")
async def export_board_cards(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
    keyword: str | None = None,
    board_code: str | None = None,
    board_name: str | None = None,
    customer_scope: str | None = None,
    return_location: str | None = None,
    material_code: str | None = None,
    material_name: str | None = None,
    status: str | None = None,
) -> Response:
    del current_user
    content = await master_data_service.export_board_cards(
        session,
        keyword=keyword,
        board_code=board_code or material_code,
        board_name=board_name or material_name,
        customer_scope=customer_scope,
        return_location=return_location,
        status=status,
    )
    return Response(
        content=content,
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="board-cards-export.xlsx"'},
    )


@router.post("/board-cards/export-selected")
async def export_selected_board_cards(
    payload: IdsRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> Response:
    del current_user
    content = await master_data_service.export_board_cards_selected(session, ids=payload.ids)
    return Response(
        content=content,
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="board-cards-selected-export.xlsx"'},
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
    items, file_hash = await asyncio.to_thread(
        master_data_service.parse_board_cards_file,
        content,
        filename=file.filename,
    )
    result = await master_data_service.import_board_cards(
        session,
        items=items,
        source_file_name=file.filename,
        source_file_hash=file_hash,
        user_id=current_user.id,
    )
    await session.commit()
    return ok(result, "board cards file imported")


@router.post("/board-cards/import-file/jobs")
async def import_board_cards_file_job(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    file: UploadFile = File(...),
) -> dict:
    return await _queue_master_data_file(session, current_user, file, kind="board_cards")


async def _queue_master_data_file(
    session: AsyncSession,
    current_user: CurrentUser,
    file: UploadFile,
    *,
    kind: str,
) -> dict:
    allowed_extensions = (".xlsx", ".xls") if kind == "board_cards" else (".xlsx",)
    if file.filename and not file.filename.lower().endswith(allowed_extensions):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MASTER_DATA_FILE_TYPE_NOT_SUPPORTED",
        )
    content = await file.read()
    if not content:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="XLSX_FILE_EMPTY")
    import hashlib

    file_hash = hashlib.sha256(content).hexdigest()
    try:
        input_object = await upload_bytes_to_oss(
            session,
            content=content,
            original_file_name=file.filename or f"{kind}.xlsx",
            content_type=master_data_service.EXCEL_MEDIA_TYPE,
            source_type="master_data_import",
            user_id=current_user.id,
        )
    except (StorageConfigurationError, StorageUploadError) as exc:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="OSS_ARCHIVAL_FAILED") from exc
    job = await enqueue_job(
        session,
        job_type="master_data_import",
        resource_type="master_data",
        resource_id=None,
        idempotency_key=f"master_data_import:{kind}:{file_hash}",
        metadata={
            "kind": kind,
            "user_id": current_user.id,
            "filename": file.filename,
        },
        input_oss_object_id=input_object.id,
    )
    await session.commit()
    return ok(serialize_job(job), "master data import queued")
