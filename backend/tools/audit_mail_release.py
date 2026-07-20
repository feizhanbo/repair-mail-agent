from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text

from app.core.database import AsyncSessionLocal, engine


CORE_TABLES = ("emails", "repair_tickets", "reply_records", "mail_fetch_records", "job_run_logs")
REQUIRED_RECEIPT_COLUMNS = (
    "device_received_at",
    "device_received_source",
    "device_received_email_id",
    "device_received_note",
    "device_received_idempotency_key",
    "device_receipt_ack_status",
)
REQUIRED_REVISION = "c0d4e5f6a7b8"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _rows(session: Any, statement: Any, **params: Any) -> list[dict[str, Any]]:
    result = await session.execute(statement, params)
    return [dict(row) for row in result.mappings().all()]


async def audit(expected_database: str, backup: Path | None) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        database_name = await session.scalar(text("SELECT DATABASE()"))
        server_version = await session.scalar(text("SELECT VERSION()"))
        revision = await session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
        table_metrics: dict[str, dict[str, int | None]] = {}
        for table in CORE_TABLES:
            row = (
                await session.execute(text(f"SELECT COUNT(*) AS row_count, MAX(id) AS max_id FROM `{table}`"))
            ).mappings().one()
            table_metrics[table] = {
                "row_count": int(row["row_count"] or 0),
                "max_id": int(row["max_id"]) if row["max_id"] is not None else None,
            }

        columns_statement = text(
            "SELECT COLUMN_NAME AS column_name, IS_NULLABLE AS is_nullable, COLUMN_DEFAULT AS column_default "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='repair_tickets' "
            "AND COLUMN_NAME IN :columns ORDER BY COLUMN_NAME"
        ).bindparams(bindparam("columns", expanding=True))
        receipt_columns = await _rows(session, columns_statement, columns=REQUIRED_RECEIPT_COLUMNS)
        uid_columns = await _rows(
            session,
            text(
                "SELECT COLUMN_NAME AS column_name, IS_NULLABLE AS is_nullable "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='mail_fetch_records' AND COLUMN_NAME='uid_validity'"
            ),
        )
        indexes = await _rows(
            session,
            text(
                "SELECT TABLE_NAME AS table_name, INDEX_NAME AS index_name, COLUMN_NAME AS column_name, "
                "SEQ_IN_INDEX AS seq_in_index, NON_UNIQUE AS non_unique "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
                "AND ((TABLE_NAME='mail_fetch_records' AND NON_UNIQUE=0 AND INDEX_NAME<>'PRIMARY') "
                "OR (TABLE_NAME='repair_tickets' AND INDEX_NAME='idx_repair_tickets_device_ack_status')) "
                "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"
            ),
        )
        foreign_keys = await _rows(
            session,
            text(
                "SELECT CONSTRAINT_NAME AS constraint_name, COLUMN_NAME AS column_name, "
                "REFERENCED_TABLE_NAME AS referenced_table_name, REFERENCED_COLUMN_NAME AS referenced_column_name "
                "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='repair_tickets' AND CONSTRAINT_NAME='fk_repair_tickets_device_received_email'"
            ),
        )
        close_transitions = await _rows(
            session,
            text(
                "SELECT trigger_event, enabled, require_manual FROM workflow_transitions "
                "WHERE from_status_code='ready_for_export' AND to_status_code='closed' ORDER BY trigger_event"
            ),
        )
        default_counts: list[dict[str, Any]] = []
        if {row["column_name"] for row in receipt_columns} == set(REQUIRED_RECEIPT_COLUMNS):
            default_counts = await _rows(
                session,
                text(
                    "SELECT device_receipt_ack_status AS status, COUNT(*) AS row_count "
                    "FROM repair_tickets GROUP BY device_receipt_ack_status ORDER BY device_receipt_ack_status"
                ),
            )
        assets = await _rows(
            session,
            text(
                "SELECT a.id, a.sn, a.customer_code, a.material_code "
                "FROM sn_assets a WHERE a.asset_status='valid' AND a.sn IS NOT NULL "
                "AND a.customer_code IS NOT NULL AND a.material_code IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM board_cards b WHERE b.material_code=a.material_code AND b.status='active') "
                "ORDER BY a.id LIMIT 1"
            ),
        )

    enabled_close_events = [row["trigger_event"] for row in close_transitions if bool(row["enabled"])]
    mail_unique_indexes: dict[str, list[str]] = {}
    for row in indexes:
        if row["table_name"] == "mail_fetch_records" and not bool(row["non_unique"]):
            mail_unique_indexes.setdefault(str(row["index_name"]), []).append(str(row["column_name"]))
    uid_unique_columns = ["mailbox_account", "folder_name", "uid_validity", "imap_uid"]
    report: dict[str, Any] = {
        "database": {
            "name": database_name,
            "server_version": server_version,
            "expected": expected_database,
            "matches_expected": database_name == expected_database,
            "revision": revision,
            "required_revision": REQUIRED_REVISION,
            "is_current": revision == REQUIRED_REVISION,
        },
        "table_metrics": table_metrics,
        "schema": {
            "uid_validity_present": bool(uid_columns),
            "receipt_columns": receipt_columns,
            "receipt_columns_complete": {row["column_name"] for row in receipt_columns}
            == set(REQUIRED_RECEIPT_COLUMNS),
            "required_indexes": indexes,
            "uid_validity_unique_constraint_present": uid_unique_columns in mail_unique_indexes.values(),
            "device_received_foreign_key": foreign_keys,
            "receipt_status_counts": default_counts,
        },
        "close_transitions": close_transitions,
        "only_device_receipt_ack_sent_enabled": enabled_close_events == ["device_receipt_ack_sent"],
        "minimum_eligible_test_asset": assets[0] if assets else None,
    }
    if backup is not None:
        resolved = backup.resolve()
        report["backup"] = {
            "path": str(resolved),
            "exists": resolved.is_file(),
            "size_bytes": resolved.stat().st_size if resolved.is_file() else None,
            "sha256": _sha256(resolved) if resolved.is_file() else None,
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only release audit for the repair mail database.")
    parser.add_argument("--expected-database", default="repair_system_test")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    async def run_audit() -> dict[str, Any]:
        try:
            return await audit(args.expected_database, args.backup)
        finally:
            await engine.dispose()

    report = asyncio.run(run_audit())
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["database"]["matches_expected"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
