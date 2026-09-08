from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.database import AsyncSessionLocal, engine
from app.models import SnAsset
from app.services.sn_master_resolution import resolve_assets


DEFAULT_SAMPLES = (
    "M81222005100008",
    "M81222005100001",
    "M81222005100002",
)


async def audit(sample_sns: list[str]) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        summary = dict(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) AS total_records, "
                        "COUNT(DISTINCT sn) AS distinct_sns, "
                        "COUNT(DISTINCT CASE WHEN asset_status = 'valid' THEN sn END) "
                        "AS valid_distinct_sns FROM sn_assets"
                    )
                )
            ).mappings().one()
        )
        duplicate_summary = dict(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) AS duplicate_sn_count, "
                        "COALESCE(MAX(record_count), 0) AS max_records_per_sn, "
                        "COALESCE(SUM(record_count - 1), 0) AS duplicate_extra_rows "
                        "FROM (SELECT sn, COUNT(*) AS record_count FROM sn_assets "
                        "GROUP BY sn HAVING COUNT(*) > 1) AS duplicated"
                    )
                )
            ).mappings().one()
        )
        duplicate_ins_ids = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM (SELECT source_system, ins_id FROM sn_assets "
                    "WHERE ins_id IS NOT NULL GROUP BY source_system, ins_id "
                    "HAVING COUNT(*) > 1) AS duplicated"
                )
            )
            or 0
        )
        samples: dict[str, Any] = {}
        for raw_sn in sample_sns:
            sn = raw_sn.strip().upper()
            rows = list(
                (
                    await session.execute(
                        select(SnAsset)
                        .where(SnAsset.sn == sn)
                        .order_by(SnAsset.warranty_end_date.desc(), SnAsset.ins_id, SnAsset.id)
                    )
                ).scalars().all()
            )
            resolution = resolve_assets(sn, rows)
            samples[sn] = resolution.snapshot()
        return {
            **summary,
            **duplicate_summary,
            "duplicate_source_ins_id_count": duplicate_ins_ids,
            "samples": samples,
        }


async def run(sample_sns: list[str]) -> None:
    try:
        print(json.dumps(await audit(sample_sns), ensure_ascii=False, indent=2, default=str))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit duplicate SN storage, row identity, and deterministic resolution."
    )
    parser.add_argument("--sn", action="append", dest="sns", help="Sample SN; repeatable")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.sns or list(DEFAULT_SAMPLES)))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "SN_MASTER_AUDIT_FAILED",
                    "exception_type": type(exc).__name__,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
