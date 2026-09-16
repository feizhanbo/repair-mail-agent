from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.sap_middleware import (
    SapMiddlewareError,
    create_sap_middleware_adapter,
)
from app.models import ExternalSyncCheckpoint, SapSnStaging, SapSnSyncBatch, SnAsset
from app.services.common import utcnow


CHECKPOINT_NAME = "sqlserver_sn_assets"


def _chunks(values: list[Any], size: int = 1000):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return date.fromisoformat(value[:10])
    return None


def assess_sn_snapshot(records: list[Any]) -> dict[str, Any]:
    counts = Counter(row.sn for row in records if row.sn)
    ins_id_counts = Counter(row.ins_id for row in records if row.ins_id is not None)
    duplicate_ins_ids = {ins_id for ins_id, count in ins_id_counts.items() if count > 1}
    invalid = [
        row
        for row in records
        if not row.sn or row.ins_id is None or not row.customer_code or not row.material_code
    ]
    invalid_ids = {id(row) for row in invalid}
    active_rows = [row for row in records if row.sn and id(row) not in invalid_ids]
    duplicate_sns = {sn for sn, count in counts.items() if count > 1}
    duplicate_rows = sum(max(0, count - 1) for count in counts.values())
    return {
        "counts": counts,
        "duplicate_sns": duplicate_sns,
        "duplicate_ins_ids": duplicate_ins_ids,
        "duplicate_count": duplicate_rows,
        "invalid": invalid,
        "valid_count": len(active_rows),
        "resolved": active_rows,
    }


def snapshot_count_change_percent(previous_count: int | None, current_count: int) -> Decimal | None:
    if not previous_count:
        return None
    return Decimal(str(abs(current_count - previous_count) * 100 / previous_count)).quantize(
        Decimal("0.0001")
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _hash(value: Any) -> str:
    raw = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _checkpoint(session: AsyncSession) -> ExternalSyncCheckpoint:
    row = await session.scalar(
        select(ExternalSyncCheckpoint).where(ExternalSyncCheckpoint.sync_name == CHECKPOINT_NAME)
    )
    if row is None:
        row = ExternalSyncCheckpoint(sync_name=CHECKPOINT_NAME)
        session.add(row)
        await session.flush()
    return row


async def sn_snapshot_freshness(session: AsyncSession) -> dict[str, Any]:
    checkpoint = await session.scalar(
        select(ExternalSyncCheckpoint).where(ExternalSyncCheckpoint.sync_name == CHECKPOINT_NAME)
    )
    if not settings.RELAY_SQLSERVER_ENABLED:
        return {"fresh": True, "status": "relay_disabled", "last_success_at": None}
    if checkpoint is None or checkpoint.last_success_at is None:
        return {"fresh": False, "status": "missing", "last_success_at": None}
    deadline = checkpoint.last_success_at + timedelta(hours=settings.RELAY_SN_SNAPSHOT_MAX_AGE_HOURS)
    return {
        "fresh": utcnow() <= deadline,
        "status": "fresh" if utcnow() <= deadline else "stale",
        "last_success_at": checkpoint.last_success_at,
        "expires_at": deadline,
    }


async def create_sn_sync_batch(
    session: AsyncSession,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    batch = SapSnSyncBatch(
        batch_no=f"SNSYNC-{utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
        status="syncing",
        started_at=utcnow(),
    )
    session.add(batch)
    await session.flush()
    checkpoint = await _checkpoint(session)
    checkpoint.last_status = "running"
    adapter = create_sap_middleware_adapter()
    try:
        records = list(await adapter.fetch_all_sn_records())
    except SapMiddlewareError as exc:
        batch.status = "failed"
        batch.error_code = str(exc).split(":", 1)[0][:100]
        batch.error_message = str(exc)[:2000]
        batch.finished_at = utcnow()
        checkpoint.last_status = "failed"
        checkpoint.last_error_code = batch.error_code
        return serialize_sync_batch(batch)

    batch.source_count = len(records)
    assessment = assess_sn_snapshot(records)
    duplicate_sns = assessment["duplicate_sns"]
    duplicate_ins_ids = assessment["duplicate_ins_ids"]
    invalid = assessment["invalid"]
    active_rows = assessment["resolved"]
    batch.duplicate_count = assessment["duplicate_count"]
    batch.invalid_count = len(invalid)
    batch.valid_count = len(active_rows)
    quarantine_message = {
        "duplicate_sns": sorted(duplicate_sns)[:50],
        "duplicate_rows_retained": assessment["duplicate_count"],
        "invalid_rows": [row.sn for row in invalid[:50]],
        "duplicate_ins_ids": sorted(duplicate_ins_ids)[:50],
    }
    if duplicate_ins_ids:
        batch.status = "failed"
        batch.error_code = "SAP_SN_DUPLICATE_INS_ID"
        batch.error_message = json.dumps(quarantine_message, ensure_ascii=False)
        batch.finished_at = utcnow()
        checkpoint.last_status = "failed"
        checkpoint.last_error_code = batch.error_code
        return serialize_sync_batch(batch)
    if invalid:
        batch.error_code = "SAP_SN_ROWS_QUARANTINED"
        batch.error_message = json.dumps(quarantine_message, ensure_ascii=False)
    elif assessment["duplicate_count"]:
        # Duplicate SN rows are expected SAP master records. Keep them all and
        # expose the count for data-quality observability.
        batch.error_message = json.dumps(quarantine_message, ensure_ascii=False)
    if not records or not active_rows:
        batch.status = "failed"
        batch.error_code = (
            "SAP_SN_SNAPSHOT_EMPTY"
            if not records
            else "SAP_SN_NO_VALID_ROWS"
        )
        batch.error_message = json.dumps(
            {
                "duplicate_sns": sorted(duplicate_sns)[:50],
                "invalid_rows": [row.sn for row in invalid[:50]],
            },
            ensure_ascii=False,
        )
        batch.finished_at = utcnow()
        checkpoint.last_status = "failed"
        checkpoint.last_error_code = batch.error_code
        return serialize_sync_batch(batch)

    protected_ins_ids = {row.ins_id for row in invalid if row.ins_id is not None}

    existing_test: set[str] = set()
    for chunk in _chunks([row.sn for row in active_rows]):
        existing_test.update(
            (
                await session.execute(
                    select(SnAsset.sn).where(
                        SnAsset.source_system == "e2e_test", SnAsset.sn.in_(chunk)
                    )
                )
            ).scalars().all()
        )
    if existing_test:
        batch.status = "failed"
        batch.error_code = "SAP_SN_E2E_SOURCE_CONFLICT"
        batch.error_message = json.dumps(sorted(existing_test)[:50], ensure_ascii=False)
        batch.finished_at = utcnow()
        checkpoint.last_status = "failed"
        checkpoint.last_error_code = batch.error_code
        return serialize_sync_batch(batch)

    previous = await session.scalar(
        select(SapSnSyncBatch)
        .where(SapSnSyncBatch.status == "succeeded", SapSnSyncBatch.id != batch.id)
        .order_by(SapSnSyncBatch.applied_at.desc(), SapSnSyncBatch.id.desc())
    )
    batch.previous_count = previous.source_count if previous else None
    batch.count_change_percent = snapshot_count_change_percent(batch.previous_count, batch.source_count)

    snapshot_rows: list[dict[str, Any]] = []
    for record in sorted(active_rows, key=lambda row: (row.sn, row.ins_id)):
        values = _json_safe(record.values)
        raw_data = _json_safe(record.raw_data)
        row_hash = _hash({"ins_id": record.ins_id, "sn": record.sn, "values": values})
        session.add(
            SapSnStaging(
                sync_batch_id=batch.id,
                ins_id=record.ins_id,
                sn=record.sn,
                customer_code=record.customer_code,
                customer_name=record.customer_name,
                material_code=record.material_code,
                material_name=record.material_name,
                asset_status=str(record.values.get("asset_status") or "valid"),
                values_json=values,
                raw_data=raw_data,
                row_hash=row_hash,
            )
        )
        snapshot_rows.append({"sn": record.sn, "ins_id": record.ins_id, "row_hash": row_hash})
    batch.snapshot_hash = _hash(snapshot_rows)
    await session.flush()

    await apply_sn_sync_batch(
        session,
        batch_id=batch.id,
        user_id=user_id,
        reason="SN 页面手动同步" if user_id else None,
        automatic=True,
        protected_ins_ids=protected_ins_ids,
    )
    return serialize_sync_batch(batch)


async def apply_sn_sync_batch(
    session: AsyncSession,
    *,
    batch_id: int,
    user_id: int | None,
    reason: str | None,
    automatic: bool = False,
    protected_ins_ids: set[int] | None = None,
) -> dict[str, Any]:
    batch = await session.get(SapSnSyncBatch, batch_id, with_for_update=True)
    if batch is None:
        raise ValueError("SAP_SN_SYNC_BATCH_NOT_FOUND")
    allowed = {"syncing"} if automatic else {"awaiting_approval"}
    if batch.status not in allowed:
        raise ValueError("SAP_SN_SYNC_BATCH_NOT_APPLICABLE")
    if not automatic and (user_id is None or not (reason or "").strip()):
        raise ValueError("SAP_SN_SYNC_APPROVAL_REASON_REQUIRED")
    staging = list(
        (
            await session.execute(
                select(SapSnStaging)
                .where(SapSnStaging.sync_batch_id == batch.id)
                .order_by(SapSnStaging.id)
            )
        ).scalars().all()
    )
    if len(staging) != batch.valid_count or not staging:
        raise ValueError("SAP_SN_STAGING_COUNT_MISMATCH")
    batch.status = "applying"
    active_ins_ids = {row.ins_id for row in staging}
    existing: dict[int, SnAsset] = {}
    for chunk in _chunks(list(active_ins_ids)):
        existing.update(
            {
                row.ins_id: row
                for row in (
                    await session.execute(
                        select(SnAsset).where(
                            SnAsset.source_system == "sqlserver",
                            SnAsset.ins_id.in_(chunk),
                        )
                    )
                ).scalars().all()
            }
        )
    for row in staging:
        asset = existing.get(row.ins_id)
        if asset is None:
            asset = SnAsset(
                sn=row.sn,
                customer_code=row.customer_code,
                customer_name=row.customer_name,
                material_code=row.material_code,
            )
            session.add(asset)
        asset.customer_code = row.customer_code
        asset.sn = row.sn
        asset.customer_name = row.customer_name
        asset.material_code = row.material_code
        asset.material_name = row.material_name
        asset.asset_status = row.asset_status
        asset.ins_id = row.ins_id
        asset.source_row_hash = row.row_hash
        for field in (
            "service_tracking_card_no",
            "parent_sn",
            "top_sn",
            "parent_material_code",
            "top_material_code",
            "warranty_start_date",
            "warranty_end_date",
        ):
            if row.values_json and field in row.values_json:
                value = row.values_json[field]
                if field in {"warranty_start_date", "warranty_end_date"} and isinstance(value, str):
                    value = _date_value(value)
                setattr(asset, field, value)
        asset.source_system = "sqlserver"
        asset.external_id = str(row.ins_id)
        asset.source_updated_at = None
        asset.raw_data = {"sqlserver": row.raw_data, "snapshot_hash": batch.snapshot_hash}
        asset.imported_at = utcnow()
    old_sqlserver = list(
        (
            await session.execute(select(SnAsset).where(SnAsset.source_system == "sqlserver"))
        ).scalars().all()
    )
    for asset in old_sqlserver:
        if asset.ins_id not in active_ins_ids and asset.ins_id not in (protected_ins_ids or set()):
            asset.asset_status = "invalid"
    batch.status = "succeeded"
    batch.approved_by_user_id = user_id
    batch.approval_reason = (reason or "").strip() or None
    batch.applied_at = utcnow()
    batch.finished_at = batch.applied_at
    checkpoint = await _checkpoint(session)
    checkpoint.cursor_value = None
    checkpoint.last_full_sync_at = batch.applied_at
    checkpoint.last_success_at = batch.applied_at
    checkpoint.last_status = "succeeded"
    checkpoint.last_error_code = None
    checkpoint.statistics_json = {
        "batch_id": batch.id,
        "source_count": batch.source_count,
        "snapshot_hash": batch.snapshot_hash,
        "count_change_percent": str(batch.count_change_percent) if batch.count_change_percent is not None else None,
    }
    return serialize_sync_batch(batch)


def serialize_sync_batch(batch: SapSnSyncBatch) -> dict[str, Any]:
    return {
        "id": batch.id,
        "batch_no": batch.batch_no,
        "status": batch.status,
        "source_count": batch.source_count,
        "valid_count": batch.valid_count,
        "invalid_count": batch.invalid_count,
        "duplicate_count": batch.duplicate_count,
        "previous_count": batch.previous_count,
        "count_change_percent": str(batch.count_change_percent) if batch.count_change_percent is not None else None,
        "snapshot_hash": batch.snapshot_hash,
        "error_code": batch.error_code,
        "error_message": batch.error_message,
        "approval_reason": batch.approval_reason,
        "approved_by_user_id": batch.approved_by_user_id,
        "started_at": batch.started_at,
        "finished_at": batch.finished_at,
        "applied_at": batch.applied_at,
    }
