from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.sap_middleware import (
    SapMiddlewareError,
    create_sap_middleware_adapter,
)
from app.models import ExternalSyncCheckpoint, JobRunLog, SapSnStaging, SapSnSyncBatch, SnAsset
from app.services.common import utcnow


CHECKPOINT_NAME = "sqlserver_sn_assets"
SN_SYNC_CHUNK_SIZE = 500


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


def _snapshot_signature(snapshot: Any) -> dict[str, int | None]:
    return {
        "source_count": int(snapshot.source_count),
        "max_ins_id": int(snapshot.max_ins_id) if snapshot.max_ins_id is not None else None,
        "duplicate_ins_id_count": int(snapshot.duplicate_ins_id_count),
        "null_ins_id_count": int(snapshot.null_ins_id_count),
    }


def _valid_staging_predicates() -> tuple[Any, ...]:
    return (
        SapSnStaging.sn != "",
        SapSnStaging.customer_code != "",
        SapSnStaging.material_code != "",
    )


async def advance_sn_sync_job(
    session: AsyncSession,
    *,
    job: JobRunLog,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Advance one durable, bounded phase of the SQL Server SN snapshot sync."""
    metadata = dict(job.metadata_json or {})
    phase = str(metadata.get("sync_phase") or "initialize")
    adapter = create_sap_middleware_adapter()

    if phase == "initialize":
        batch = SapSnSyncBatch(
            batch_no=f"SNSYNC-{utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
            status="syncing",
            started_at=utcnow(),
        )
        session.add(batch)
        await session.flush()
        checkpoint = await _checkpoint(session)
        checkpoint.last_status = "running"
        try:
            snapshot = await adapter.inspect_sn_snapshot()
        except SapMiddlewareError as exc:
            return _fail_sync_batch(batch, checkpoint, exc)
        signature = _snapshot_signature(snapshot)
        batch.source_count = int(snapshot.source_count)
        if snapshot.null_ins_id_count or snapshot.duplicate_ins_id_count:
            code = (
                "SAP_SN_NULL_INS_ID"
                if snapshot.null_ins_id_count
                else "SAP_SN_DUPLICATE_INS_ID"
            )
            return _fail_sync_batch(batch, checkpoint, RuntimeError(code), details=signature)
        if not snapshot.source_count or snapshot.max_ins_id is None:
            return _fail_sync_batch(
                batch, checkpoint, RuntimeError("SAP_SN_SNAPSHOT_EMPTY"), details=signature
            )
        previous = await session.scalar(
            select(SapSnSyncBatch)
            .where(SapSnSyncBatch.status == "succeeded", SapSnSyncBatch.id != batch.id)
            .order_by(SapSnSyncBatch.applied_at.desc(), SapSnSyncBatch.id.desc())
        )
        batch.previous_count = previous.source_count if previous else None
        batch.count_change_percent = snapshot_count_change_percent(
            batch.previous_count, batch.source_count
        )
        job.metadata_json = {
            **metadata,
            "sync_phase": "stage",
            "sync_batch_id": batch.id,
            "sync_snapshot": signature,
            "sync_cursor": None,
            "sync_staged_count": 0,
            "sync_hash": _hash({"batch_id": batch.id, **signature}),
            "user_id": user_id,
        }
        job.processed_count = 0
        return {"status": "chunk_pending", "phase": "stage", "batch_id": batch.id}

    batch_id = int(metadata.get("sync_batch_id") or 0)
    batch = await session.get(SapSnSyncBatch, batch_id, with_for_update=True)
    if batch is None:
        raise ValueError("SAP_SN_SYNC_BATCH_NOT_FOUND")
    if batch.status == "failed":
        return serialize_sync_batch(batch)
    if batch.status == "succeeded":
        return serialize_sync_batch(batch)

    if phase == "stage":
        signature = dict(metadata.get("sync_snapshot") or {})
        page = list(
            await adapter.fetch_sn_records_page(
                after_ins_id=(
                    int(metadata["sync_cursor"])
                    if metadata.get("sync_cursor") is not None
                    else None
                ),
                max_ins_id=int(signature["max_ins_id"]),
                limit=SN_SYNC_CHUNK_SIZE,
            )
        )
        rolling_hash = str(metadata.get("sync_hash") or "")
        for record in page:
            if record.ins_id is None:
                raise ValueError("SAP_SN_NULL_INS_ID")
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
            rolling_hash = _hash({"previous": rolling_hash, "row_hash": row_hash})
        await session.flush()
        staged_count = int(metadata.get("sync_staged_count") or 0) + len(page)
        next_phase = "validate" if len(page) < SN_SYNC_CHUNK_SIZE else "stage"
        job.metadata_json = {
            **metadata,
            "sync_phase": next_phase,
            "sync_cursor": page[-1].ins_id if page else metadata.get("sync_cursor"),
            "sync_staged_count": staged_count,
            "sync_hash": rolling_hash,
        }
        job.processed_count = staged_count
        return {
            "status": "chunk_pending",
            "phase": next_phase,
            "batch_id": batch.id,
            "staged_count": staged_count,
        }

    if phase == "validate":
        expected = dict(metadata.get("sync_snapshot") or {})
        current = _snapshot_signature(await adapter.inspect_sn_snapshot())
        staged_count = int(
            await session.scalar(
                select(func.count()).select_from(SapSnStaging).where(
                    SapSnStaging.sync_batch_id == batch.id
                )
            )
            or 0
        )
        if current != expected or staged_count != int(expected.get("source_count") or 0):
            checkpoint = await _checkpoint(session)
            return _fail_sync_batch(
                batch,
                checkpoint,
                RuntimeError("SAP_SN_SNAPSHOT_UNSTABLE"),
                details={"expected": expected, "current": current, "staged_count": staged_count},
            )
        invalid_predicate = ~(
            (SapSnStaging.sn != "")
            & (SapSnStaging.customer_code != "")
            & (SapSnStaging.material_code != "")
        )
        batch.invalid_count = int(
            await session.scalar(
                select(func.count()).select_from(SapSnStaging).where(
                    SapSnStaging.sync_batch_id == batch.id, invalid_predicate
                )
            )
            or 0
        )
        batch.valid_count = staged_count - batch.invalid_count
        duplicate_groups = (
            await session.execute(
                select(func.count(SapSnStaging.id))
                .where(
                    SapSnStaging.sync_batch_id == batch.id,
                    SapSnStaging.sn != "",
                )
                .group_by(SapSnStaging.sn)
                .having(func.count(SapSnStaging.id) > 1)
            )
        ).scalars().all()
        batch.duplicate_count = sum(int(count) - 1 for count in duplicate_groups)
        batch.snapshot_hash = str(metadata.get("sync_hash") or "") or None
        if batch.valid_count <= 0:
            checkpoint = await _checkpoint(session)
            return _fail_sync_batch(batch, checkpoint, RuntimeError("SAP_SN_NO_VALID_ROWS"))
        conflict = await session.scalar(
            select(SnAsset.sn)
            .where(
                SnAsset.source_system == "e2e_test",
                SnAsset.sn.in_(
                    select(SapSnStaging.sn).where(
                        SapSnStaging.sync_batch_id == batch.id,
                        *_valid_staging_predicates(),
                    )
                ),
            )
            .limit(1)
        )
        if conflict:
            checkpoint = await _checkpoint(session)
            return _fail_sync_batch(
                batch, checkpoint, RuntimeError("SAP_SN_E2E_SOURCE_CONFLICT"), details={"sn": conflict}
            )
        if (
            batch.count_change_percent is not None
            and batch.count_change_percent
            > Decimal(str(settings.RELAY_SN_COUNT_CHANGE_GUARD_PERCENT))
        ):
            batch.status = "awaiting_approval"
            batch.error_code = "SAP_SN_COUNT_CHANGE_REQUIRES_APPROVAL"
            batch.error_message = json.dumps(
                {
                    "previous_count": batch.previous_count,
                    "source_count": batch.source_count,
                    "count_change_percent": str(batch.count_change_percent),
                },
                ensure_ascii=False,
            )
            return {
                **serialize_sync_batch(batch),
                "status": "pending_review",
                "batch_status": "awaiting_approval",
            }
        batch.status = "applying"
        job.metadata_json = {**metadata, "sync_phase": "apply", "sync_apply_cursor": 0}
        return {"status": "chunk_pending", "phase": "apply", "batch_id": batch.id}

    if phase == "apply":
        if batch.status == "awaiting_approval":
            batch.status = "applying"
            batch.error_code = None
            batch.error_message = None
        cursor = int(metadata.get("sync_apply_cursor") or 0)
        staging = list(
            (
                await session.execute(
                    select(SapSnStaging)
                    .where(
                        SapSnStaging.sync_batch_id == batch.id,
                        SapSnStaging.id > cursor,
                        *_valid_staging_predicates(),
                    )
                    .order_by(SapSnStaging.id)
                    .limit(SN_SYNC_CHUNK_SIZE)
                )
            ).scalars().all()
        )
        if staging:
            existing = {
                row.ins_id: row
                for row in (
                    await session.execute(
                        select(SnAsset).where(
                            SnAsset.source_system == "sqlserver",
                            SnAsset.ins_id.in_([row.ins_id for row in staging]),
                        )
                    )
                ).scalars().all()
            }
            for row in staging:
                _apply_staging_row(session, existing.get(row.ins_id), row, batch)
            cursor = int(staging[-1].id)
            job.metadata_json = {**metadata, "sync_apply_cursor": cursor}
            return {
                "status": "chunk_pending",
                "phase": "apply",
                "batch_id": batch.id,
                "applied_through_id": cursor,
            }
        job.metadata_json = {**metadata, "sync_phase": "finalize"}
        return {"status": "chunk_pending", "phase": "finalize", "batch_id": batch.id}

    if phase == "finalize":
        staged_ins_ids = select(SapSnStaging.ins_id).where(
            SapSnStaging.sync_batch_id == batch.id
        )
        await session.execute(
            update(SnAsset)
            .where(
                SnAsset.source_system == "sqlserver",
                or_(
                    SnAsset.ins_id.is_(None),
                    SnAsset.ins_id.not_in(staged_ins_ids),
                ),
            )
            .values(asset_status="invalid")
        )
        batch.status = "succeeded"
        batch.approved_by_user_id = user_id
        batch.approval_reason = (
            str(metadata.get("approval_reason") or "").strip()
            or ("SN 页面手动同步" if user_id else None)
        )
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
            "count_change_percent": (
                str(batch.count_change_percent) if batch.count_change_percent is not None else None
            ),
        }
        return serialize_sync_batch(batch)

    raise ValueError("SAP_SN_SYNC_PHASE_INVALID")


def _apply_staging_row(
    session: AsyncSession,
    asset: SnAsset | None,
    row: SapSnStaging,
    batch: SapSnSyncBatch,
) -> SnAsset:
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
    return asset


def _fail_sync_batch(
    batch: SapSnSyncBatch,
    checkpoint: ExternalSyncCheckpoint,
    exc: BaseException,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = str(exc).split(":", 1)[0][:100] or "SAP_SN_SYNC_FAILED"
    batch.status = "failed"
    batch.error_code = code
    batch.error_message = json.dumps(details, ensure_ascii=False) if details else str(exc)[:2000]
    batch.finished_at = utcnow()
    checkpoint.last_status = "failed"
    checkpoint.last_error_code = code
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
