from __future__ import annotations

import asyncio
import re
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok, page
from app.models import SapSnSyncBatch
from app.schemas.business import (
    BoardCardImportRequest,
    BoardCardUpdateRequest,
    CustomerServicePolicyCreateRequest,
    CustomerServicePolicyUpdateRequest,
    IdsRequest,
    SnAssetImportRequest,
    SnAssetUpdateRequest,
    SnSyncConfigUpdateRequest,
)
from app.services.audit import log_operation
from app.services.external_relay import relay_configuration_status
from app.services.runtime_config import apply_runtime_config, load_runtime_config, persist_runtime_config, read_runtime_config
from app.services.sap_sn_sync import create_sn_sync_batch, serialize_sync_batch
from app.services import customer_policies
from app.services import master_data as master_data_service
from app.services.jobs import enqueue_job, serialize_job
from app.services.storage import StorageConfigurationError, StorageUploadError, upload_bytes_to_oss

router = APIRouter()

_SN_LOCAL_FIELDS = {
    "sn", "customer_code", "customer_name", "material_code", "material_name",
    "asset_status", "service_tracking_card_no", "parent_sn", "top_sn",
    "parent_material_code", "top_material_code", "warranty_start_date", "warranty_end_date",
}
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sn_sync_config_payload() -> dict:
    runtime = read_runtime_config()
    return {
        "relay_sqlserver_enabled": runtime["relay_sqlserver_enabled"],
        "relay_sn_sync_enabled": runtime["relay_sn_sync_enabled"],
        "sn_schema": runtime["sn_schema"],
        "sn_table": runtime["sn_table"],
        "sn_primary_key": runtime["sn_primary_key"],
        "sn_updated_at_column": runtime["sn_updated_at_column"],
        "sn_column_map": runtime["sn_column_map"],
        "batch_size": runtime["batch_size"],
        "snapshot_max_age_hours": runtime["snapshot_max_age_hours"],
        "connection": relay_configuration_status(),
    }


@router.get("/sn-sync/config", deprecated=True, description="Compatibility alias; use /system/sn-sync/config.")
async def get_sn_sync_config(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    del current_user
    await load_runtime_config(session)
    return ok(_sn_sync_config_payload())


@router.patch("/sn-sync/config", deprecated=True, description="Compatibility alias; use /system/sn-sync/config.")
async def update_sn_sync_config(
    payload: SnSyncConfigUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    values = payload.model_dump(exclude_unset=True)
    column_map = values.get("sn_column_map")
    if column_map is not None:
        if set(column_map) - _SN_LOCAL_FIELDS or any(not _IDENTIFIER.fullmatch(str(value)) for value in column_map.values()):
            from fastapi import HTTPException, status
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SN_SYNC_COLUMN_MAP_INVALID")
    await load_runtime_config(session)
    before = _sn_sync_config_payload()
    next_config = await persist_runtime_config(session, values, user_id=current_user.id)
    apply_runtime_config(next_config)
    after = _sn_sync_config_payload()
    await log_operation(session, user_id=current_user.id, operation_type="sn_sync_config_updated", target_type="sn_sync_config", description="用户确认更新SN同步配置", before_data=before, after_data=after)
    await session.commit()
    return ok(after, "SN sync config updated")


@router.post("/sn-sync", deprecated=True, description="Compatibility alias; use /system/sn-sync.")
async def start_sn_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    result = await create_sn_sync_batch(session, user_id=current_user.id)
    await log_operation(session, user_id=current_user.id, operation_type="sn_sync_executed", target_type="sap_sn_sync_batch", target_id=result.get("id"), description="SN full snapshot requested from master-data page", after_data=result)
    await session.commit()
    return ok(result, "SN snapshot synchronized")


@router.get("/sn-sync/latest", deprecated=True, description="Compatibility alias; use /system/sn-sync/latest.")
async def latest_sn_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    del current_user
    batch = await session.scalar(select(SapSnSyncBatch).order_by(SapSnSyncBatch.id.desc()).limit(1))
    return ok(serialize_sync_batch(batch) if batch else None)


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
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    values = payload.model_dump(exclude_unset=True)
    reason = values.pop("reason", payload.reason)
    result = await customer_policies.update_policy(
        session,
        policy_id=policy_id,
        values=values,
        user_id=current_user.id,
        reason=reason,
    )
    await session.commit()
    return ok(result, "customer policy updated")


@router.delete("/customer-policies/{policy_id}")
async def delete_customer_policy(
    policy_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    result = await customer_policies.delete_policy(session, policy_id=policy_id, user_id=current_user.id, reason="用户确认删除客户政策")
    await session.commit()
    return ok(result, "customer policy deleted")


@router.get("/customer-policies/{policy_id}/delete-preview")
async def customer_policy_delete_preview(policy_id: int, session: Annotated[AsyncSession, Depends(get_session)], current_user: Annotated[CurrentUser, Depends(require_roles("operator"))]) -> dict:
    del current_user
    return ok(await customer_policies.preview_policy_delete(session, policy_id))


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


@router.patch("/sn-assets/{asset_id}")
async def update_sn_asset(
    asset_id: int,
    payload: SnAssetUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    values = payload.model_dump(exclude_unset=True)
    reason = values.pop("reason")
    result = await master_data_service.update_sn_asset(session, asset_id=asset_id, values=values, user_id=current_user.id, reason=reason)
    await session.commit()
    return ok(result, "sn asset updated")


@router.delete("/sn-assets/{asset_id}")
async def delete_sn_asset(
    asset_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    result = await master_data_service.delete_sn_asset(session, asset_id=asset_id, user_id=current_user.id, reason="用户确认删除SN资料")
    await session.commit()
    return ok(result, "sn asset deleted")


@router.get("/sn-assets/{asset_id}/delete-preview")
async def sn_asset_delete_preview(asset_id: int, session: Annotated[AsyncSession, Depends(get_session)], current_user: Annotated[CurrentUser, Depends(require_roles("operator"))]) -> dict:
    del current_user
    return ok(await master_data_service.preview_sn_asset_delete(session, asset_id))


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
    content = await master_data_service.export_sn_assets(
        session,
        keyword=keyword,
        sn=sn,
        customer=customer,
        material=material,
        asset_status=asset_status,
    )
    await log_operation(
        session, user_id=current_user.id, operation_type="sn_assets_exported",
        target_type="sn_asset_export", description="用户导出 SN 主数据。",
        after_data={"filter_keys": sorted(key for key, value in {
            "keyword": keyword, "sn": sn, "customer": customer,
            "material": material, "asset_status": asset_status,
        }.items() if value is not None)},
    )
    await session.commit()
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
    content = await master_data_service.export_sn_assets_selected(session, ids=payload.ids)
    await log_operation(
        session, user_id=current_user.id, operation_type="sn_assets_exported",
        target_type="sn_asset_export", description="用户导出选中的 SN 主数据。",
        after_data={"selected_count": len(payload.ids), "selected_ids": payload.ids},
    )
    await session.commit()
    return Response(
        content=content,
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="sn-assets-selected-export.xlsx"'},
    )


@router.post("/sn-assets/import")
async def import_sn_assets(
    payload: SnAssetImportRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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


@router.patch("/board-cards/{card_id}")
async def update_board_card(
    card_id: int,
    payload: BoardCardUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    values = payload.model_dump(exclude_unset=True)
    reason = values.pop("reason")
    result = await master_data_service.update_board_card(session, card_id=card_id, values=values, user_id=current_user.id, reason=reason)
    await session.commit()
    return ok(result, "board card updated")


@router.delete("/board-cards/{card_id}")
async def delete_board_card(
    card_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    result = await master_data_service.delete_board_card(session, card_id=card_id, user_id=current_user.id, reason="用户确认删除板卡资料")
    await session.commit()
    return ok(result, "board card deleted")


@router.get("/board-cards/{card_id}/delete-preview")
async def board_card_delete_preview(card_id: int, session: Annotated[AsyncSession, Depends(get_session)], current_user: Annotated[CurrentUser, Depends(require_roles("operator"))]) -> dict:
    del current_user
    return ok(await master_data_service.preview_board_card_delete(session, card_id))


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
    content = await master_data_service.export_board_cards(
        session,
        keyword=keyword,
        board_code=board_code or material_code,
        board_name=board_name or material_name,
        customer_scope=customer_scope,
        return_location=return_location,
        status=status,
    )
    await log_operation(
        session, user_id=current_user.id, operation_type="board_cards_exported",
        target_type="board_card_export", description="用户导出板卡主数据。",
        after_data={"filter_keys": sorted(key for key, value in {
            "keyword": keyword, "board_code": board_code or material_code,
            "board_name": board_name or material_name, "customer_scope": customer_scope,
            "return_location": return_location, "status": status,
        }.items() if value is not None)},
    )
    await session.commit()
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
    content = await master_data_service.export_board_cards_selected(session, ids=payload.ids)
    await log_operation(
        session, user_id=current_user.id, operation_type="board_cards_exported",
        target_type="board_card_export", description="用户导出选中的板卡主数据。",
        after_data={"selected_count": len(payload.ids), "selected_ids": payload.ids},
    )
    await session.commit()
    return Response(
        content=content,
        media_type=master_data_service.EXCEL_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="board-cards-selected-export.xlsx"'},
    )


@router.post("/board-cards/import")
async def import_board_cards(
    payload: BoardCardImportRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
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
