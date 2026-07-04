from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BoardCard, JobRunLog, SnAsset
from app.schemas.business import BoardCardImportItem, SnAssetImportItem
from app.services.audit import log_operation
from app.services.common import model_to_dict, paginate_scalars, utcnow

SN_ASSET_FIELDS = (
    "id",
    "customer_code",
    "customer_name",
    "material_code",
    "material_name",
    "sn",
    "asset_status",
    "warranty_start_date",
    "warranty_end_date",
    "source_file_name",
    "source_file_hash",
    "source_row_no",
    "raw_data",
    "imported_by_user_id",
    "imported_at",
    "created_at",
    "updated_at",
)

BOARD_CARD_FIELDS = (
    "id",
    "material_code",
    "material_name",
    "need_ship_to_beijing",
    "shipping_address",
    "shipping_contact",
    "shipping_phone",
    "postal_code",
    "status",
    "source_file_name",
    "source_file_hash",
    "source_row_no",
    "raw_data",
    "imported_by_user_id",
    "imported_at",
    "created_at",
    "updated_at",
)


async def list_sn_assets(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
    asset_status: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    statement = select(SnAsset)
    if asset_status:
        statement = statement.where(SnAsset.asset_status == asset_status)
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(
            or_(SnAsset.sn.like(like), SnAsset.customer_code.like(like), SnAsset.customer_name.like(like), SnAsset.material_code.like(like))
        )
    statement = statement.order_by(SnAsset.updated_at.desc(), SnAsset.id.desc())
    rows, total = await paginate_scalars(session, statement, page, page_size)
    return [model_to_dict(row, SN_ASSET_FIELDS) for row in rows], total


async def import_sn_assets(
    session: AsyncSession,
    *,
    items: list[SnAssetImportItem],
    source_file_name: str | None,
    source_file_hash: str | None,
    user_id: int,
) -> dict[str, Any]:
    job = JobRunLog(job_name="sn_assets_import", job_type="master_data_import", status="running", processed_count=len(items), metadata_json={})
    session.add(job)
    await session.flush()
    created = 0
    updated = 0
    for item in items:
        data = item.model_dump()
        sn = data["sn"].strip().upper()
        row = await session.scalar(select(SnAsset).where(SnAsset.sn == sn))
        payload = {
            **data,
            "sn": sn,
            "source_file_name": source_file_name,
            "source_file_hash": source_file_hash,
            "imported_by_user_id": user_id,
            "imported_at": utcnow(),
        }
        if row is None:
            session.add(SnAsset(**payload))
            created += 1
        else:
            for key, value in payload.items():
                setattr(row, key, value)
            updated += 1
    job.status = "success"
    job.finished_at = utcnow()
    job.success_count = created + updated
    job.metadata_json = {"created": created, "updated": updated, "source_file_name": source_file_name}
    await log_operation(
        session,
        user_id=user_id,
        operation_type="sn_assets_imported",
        target_type="sn_assets",
        target_id=None,
        after_data=job.metadata_json,
    )
    return {"job_run_id": job.id, "created": created, "updated": updated, "processed": len(items)}


async def list_board_cards(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    statement = select(BoardCard)
    if status:
        statement = statement.where(BoardCard.status == status)
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(or_(BoardCard.material_code.like(like), BoardCard.material_name.like(like)))
    statement = statement.order_by(BoardCard.updated_at.desc(), BoardCard.id.desc())
    rows, total = await paginate_scalars(session, statement, page, page_size)
    return [model_to_dict(row, BOARD_CARD_FIELDS) for row in rows], total


async def import_board_cards(
    session: AsyncSession,
    *,
    items: list[BoardCardImportItem],
    source_file_name: str | None,
    source_file_hash: str | None,
    user_id: int,
) -> dict[str, Any]:
    job = JobRunLog(job_name="board_cards_import", job_type="master_data_import", status="running", processed_count=len(items), metadata_json={})
    session.add(job)
    await session.flush()
    created = 0
    updated = 0
    for item in items:
        data = item.model_dump()
        material_code = data["material_code"].strip()
        row = await session.scalar(select(BoardCard).where(BoardCard.material_code == material_code))
        payload = {
            **data,
            "material_code": material_code,
            "source_file_name": source_file_name,
            "source_file_hash": source_file_hash,
            "imported_by_user_id": user_id,
            "imported_at": utcnow(),
        }
        if row is None:
            session.add(BoardCard(**payload))
            created += 1
        else:
            for key, value in payload.items():
                setattr(row, key, value)
            updated += 1
    job.status = "success"
    job.finished_at = utcnow()
    job.success_count = created + updated
    job.metadata_json = {"created": created, "updated": updated, "source_file_name": source_file_name}
    await log_operation(
        session,
        user_id=user_id,
        operation_type="board_cards_imported",
        target_type="board_cards",
        target_id=None,
        after_data=job.metadata_json,
    )
    return {"job_run_id": job.id, "created": created, "updated": updated, "processed": len(items)}
