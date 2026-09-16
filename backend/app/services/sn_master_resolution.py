from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SnAsset


RESOLVED = "RESOLVED"
SN_NOT_FOUND = "SN_NOT_FOUND"
MASTER_DATA_UNRESOLVED = "MASTER_DATA_UNRESOLVED"
MASTER_DATA_AMBIGUOUS = "MASTER_DATA_AMBIGUOUS"

LATEST_DATE = "LATEST_DATE"
LATEST_DATE_AND_FINISHED_PRODUCT = "LATEST_DATE_AND_FINISHED_PRODUCT"
LATEST_DATE_AND_A_PREFERRED = "LATEST_DATE_AND_A_PREFERRED"
MANUAL = "MANUAL"


def normalize_sn(value: Any) -> str:
    return str(value or "").strip().upper()


def is_semi_finished(asset: SnAsset) -> bool:
    return str(asset.material_code or "").strip().upper().startswith("B.SM.")


def _is_a_board(asset: SnAsset) -> bool:
    code = str(asset.material_code or "").strip().upper()
    return code.endswith("A") and not code.endswith("AB")


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def asset_snapshot(asset: SnAsset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "ins_id": asset.ins_id,
        "sn": normalize_sn(asset.sn),
        "customer_code": asset.customer_code,
        "customer_name": asset.customer_name,
        "material_code": asset.material_code,
        "material_name": asset.material_name,
        "service_tracking_card_no": asset.service_tracking_card_no,
        "parent_sn": asset.parent_sn,
        "top_sn": asset.top_sn,
        "parent_material_code": asset.parent_material_code,
        "top_material_code": asset.top_material_code,
        "warranty_start_date": asset.warranty_start_date.isoformat()
        if asset.warranty_start_date
        else None,
        "warranty_end_date": asset.warranty_end_date.isoformat()
        if asset.warranty_end_date
        else None,
        "asset_status": asset.asset_status,
        "source_system": asset.source_system,
        "source_row_hash": asset.source_row_hash,
    }


@dataclass(frozen=True)
class ResolutionResult:
    sn: str
    status: str
    asset: SnAsset | None
    method: str | None
    reason: str
    candidates: tuple[SnAsset, ...]
    all_records: tuple[SnAsset, ...]

    def snapshot(self) -> dict[str, Any]:
        return {
            "sn": self.sn,
            "status": self.status,
            "method": self.method,
            "reason": self.reason,
            "resolved_asset": asset_snapshot(self.asset) if self.asset else None,
            "candidate_ids": [row.id for row in self.candidates],
            "candidates": [asset_snapshot(row) for row in self.candidates],
            "record_count": len(self.all_records),
        }


@dataclass(frozen=True)
class ResolvedAssetSnapshot:
    id: int
    ins_id: int
    sn: str
    customer_code: str
    customer_name: str | None
    material_code: str
    material_name: str | None
    warranty_start_date: date | None
    warranty_end_date: date | None
    service_tracking_card_no: str | None
    parent_sn: str | None
    top_sn: str | None
    parent_material_code: str | None
    top_material_code: str | None
    source_row_hash: str | None


def resolved_asset_from_snapshot(snapshot: dict[str, Any] | None) -> ResolvedAssetSnapshot | None:
    values = (snapshot or {}).get("resolved_asset")
    if not isinstance(values, dict):
        return None
    try:
        return ResolvedAssetSnapshot(
            id=int(values["id"]),
            ins_id=int(values["ins_id"]),
            sn=normalize_sn(values["sn"]),
            customer_code=str(values["customer_code"]),
            customer_name=values.get("customer_name"),
            material_code=str(values["material_code"]),
            material_name=values.get("material_name"),
            warranty_start_date=_date(values.get("warranty_start_date")),
            warranty_end_date=_date(values.get("warranty_end_date")),
            service_tracking_card_no=values.get("service_tracking_card_no"),
            parent_sn=values.get("parent_sn"),
            top_sn=values.get("top_sn"),
            parent_material_code=values.get("parent_material_code"),
            top_material_code=values.get("top_material_code"),
            source_row_hash=values.get("source_row_hash"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def resolve_assets(sn: str, records: Iterable[SnAsset]) -> ResolutionResult:
    normalized = normalize_sn(sn)
    all_records = tuple(row for row in records if normalize_sn(row.sn) == normalized)
    if not all_records:
        return ResolutionResult(
            normalized, SN_NOT_FOUND, None, None, "SN_NOT_FOUND", (), ()
        )

    usable = tuple(
        row
        for row in all_records
        if row.asset_status == "valid"
        and isinstance(row.ins_id, int)
        and bool(str(row.customer_code or "").strip())
        and bool(str(row.material_code or "").strip())
    )
    if not usable:
        return ResolutionResult(
            normalized,
            MASTER_DATA_UNRESOLVED,
            None,
            None,
            "NO_VALID_COMPLETE_MASTER_RECORD",
            (),
            all_records,
        )

    dated = [(row, _date(row.warranty_end_date)) for row in usable]
    if not any(expiry is not None for _, expiry in dated):
        return ResolutionResult(
            normalized,
            MASTER_DATA_UNRESOLVED,
            None,
            None,
            "EXP_DATE_ALL_NULL",
            usable,
            all_records,
        )
    latest_date = max(expiry for _, expiry in dated if expiry is not None)
    latest = tuple(row for row, expiry in dated if expiry == latest_date)
    finished = tuple(row for row in latest if not is_semi_finished(row))
    if not finished:
        return ResolutionResult(
            normalized,
            MASTER_DATA_UNRESOLVED,
            None,
            None,
            "LATEST_DATE_ONLY_SEMI_FINISHED",
            latest,
            all_records,
        )
    if len(finished) == 1:
        method = (
            LATEST_DATE_AND_FINISHED_PRODUCT
            if len(finished) != len(latest)
            else LATEST_DATE
        )
        return ResolutionResult(
            normalized, RESOLVED, finished[0], method, "UNIQUE_CANDIDATE", finished, all_records
        )

    a_boards = tuple(row for row in finished if _is_a_board(row))
    if len(a_boards) == 1:
        return ResolutionResult(
            normalized,
            RESOLVED,
            a_boards[0],
            LATEST_DATE_AND_A_PREFERRED,
            "A_PREFERRED_OVER_AB",
            finished,
            all_records,
        )
    if len(a_boards) > 1:
        return ResolutionResult(
            normalized,
            MASTER_DATA_AMBIGUOUS,
            None,
            None,
            "MULTIPLE_A_CANDIDATES",
            a_boards,
            all_records,
        )

    return ResolutionResult(
        normalized,
        MASTER_DATA_AMBIGUOUS,
        None,
        None,
        "MULTIPLE_NON_A_CANDIDATES",
        finished,
        all_records,
    )


async def find_assets_by_sn(session: AsyncSession, sn: str) -> list[SnAsset]:
    normalized = normalize_sn(sn)
    return list(
        (
            await session.execute(
                select(SnAsset)
                .where(SnAsset.sn == normalized)
                .order_by(SnAsset.warranty_end_date.desc(), SnAsset.ins_id, SnAsset.id)
            )
        ).scalars().all()
    )


async def sn_exists(session: AsyncSession, sn: str) -> bool:
    return (
        await session.scalar(select(SnAsset.id).where(SnAsset.sn == normalize_sn(sn)).limit(1))
    ) is not None


async def resolve_sn_asset(session: AsyncSession, sn: str) -> ResolutionResult:
    return resolve_assets(sn, await find_assets_by_sn(session, sn))
