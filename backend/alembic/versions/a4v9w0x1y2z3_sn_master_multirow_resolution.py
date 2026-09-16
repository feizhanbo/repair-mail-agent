"""Allow multiple SAP master rows per business SN.

Revision ID: a4v9w0x1y2z3
Revises: z3u8v9w0x1y2
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql


revision: str = "a4v9w0x1y2z3"
down_revision: str | None = "z3u8v9w0x1y2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _count(sql: str) -> int:
    return int(op.get_bind().execute(sa.text(sql)).scalar_one() or 0)


def upgrade() -> None:
    if not context.is_offline_mode():
        if _count(
            "SELECT COUNT(*) FROM ("
            "SELECT source_system, ins_id FROM sn_assets WHERE ins_id IS NOT NULL "
            "GROUP BY source_system, ins_id HAVING COUNT(*) > 1"
            ") AS duplicate_ins_ids"
        ):
            raise RuntimeError("SN_ASSET_SOURCE_INS_ID_DUPLICATES")
        if _count(
            "SELECT COUNT(*) FROM ("
            "SELECT source_system, external_id FROM sn_assets WHERE external_id IS NOT NULL "
            "GROUP BY source_system, external_id HAVING COUNT(*) > 1"
            ") AS duplicate_external_ids"
        ):
            raise RuntimeError("SN_ASSET_EXTERNAL_ID_DUPLICATES")

    op.drop_constraint("uk_sn_assets_sn", "sn_assets", type_="unique")
    op.create_index("idx_sn_assets_sn", "sn_assets", ["sn"], unique=False)
    op.create_unique_constraint(
        "uk_sn_assets_source_ins_id", "sn_assets", ["source_system", "ins_id"]
    )
    op.drop_index("idx_sn_assets_external", table_name="sn_assets")
    op.create_unique_constraint(
        "uk_sn_assets_external", "sn_assets", ["source_system", "external_id"]
    )

    op.add_column("sap_sn_staging", sa.Column("ins_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE sap_sn_staging SET ins_id = CAST("
        "JSON_UNQUOTE(JSON_EXTRACT(values_json, '$.ins_id')) AS SIGNED) "
        "WHERE ins_id IS NULL AND JSON_EXTRACT(values_json, '$.ins_id') IS NOT NULL"
    )
    if not context.is_offline_mode() and _count(
        "SELECT COUNT(*) FROM sap_sn_staging WHERE ins_id IS NULL"
    ):
        raise RuntimeError("SAP_SN_STAGING_INS_ID_MISSING")
    op.alter_column(
        "sap_sn_staging", "ins_id", existing_type=sa.Integer(), nullable=False
    )
    op.drop_constraint(
        "uk_sap_sn_staging_batch_sn", "sap_sn_staging", type_="unique"
    )
    op.create_unique_constraint(
        "uk_sap_sn_staging_batch_ins_id",
        "sap_sn_staging",
        ["sync_batch_id", "ins_id"],
    )
    op.create_index(
        "idx_sap_sn_staging_batch_sn",
        "sap_sn_staging",
        ["sync_batch_id", "sn"],
        unique=False,
    )

    op.add_column(
        "repair_ticket_items",
        sa.Column(
            "sn_master_resolution_status",
            sa.String(length=30),
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column(
        "repair_ticket_items",
        sa.Column("sn_master_resolution_method", sa.String(length=60), nullable=True),
    )
    op.add_column(
        "repair_ticket_items",
        sa.Column("sn_master_resolution_snapshot", mysql.JSON(), nullable=True),
    )
    op.add_column(
        "repair_ticket_items",
        sa.Column("sn_master_resolved_at", mysql.DATETIME(fsp=3), nullable=True),
    )


def downgrade() -> None:
    if not context.is_offline_mode() and _count(
        "SELECT COUNT(*) FROM (SELECT sn FROM sn_assets GROUP BY sn HAVING COUNT(*) > 1) AS d"
    ):
        raise RuntimeError("CANNOT_RESTORE_SN_UNIQUE_WITH_DUPLICATE_ROWS")
    if not context.is_offline_mode() and _count(
        "SELECT COUNT(*) FROM ("
        "SELECT sync_batch_id, sn FROM sap_sn_staging "
        "GROUP BY sync_batch_id, sn HAVING COUNT(*) > 1"
        ") AS d"
    ):
        raise RuntimeError("CANNOT_RESTORE_STAGING_SN_UNIQUE_WITH_DUPLICATE_ROWS")

    op.drop_column("repair_ticket_items", "sn_master_resolved_at")
    op.drop_column("repair_ticket_items", "sn_master_resolution_snapshot")
    op.drop_column("repair_ticket_items", "sn_master_resolution_method")
    op.drop_column("repair_ticket_items", "sn_master_resolution_status")

    op.drop_index("idx_sap_sn_staging_batch_sn", table_name="sap_sn_staging")
    op.drop_constraint(
        "uk_sap_sn_staging_batch_ins_id", "sap_sn_staging", type_="unique"
    )
    op.create_unique_constraint(
        "uk_sap_sn_staging_batch_sn", "sap_sn_staging", ["sync_batch_id", "sn"]
    )
    op.drop_column("sap_sn_staging", "ins_id")

    op.drop_constraint("uk_sn_assets_external", "sn_assets", type_="unique")
    op.create_index(
        "idx_sn_assets_external",
        "sn_assets",
        ["source_system", "external_id"],
        unique=False,
    )
    op.drop_constraint("uk_sn_assets_source_ins_id", "sn_assets", type_="unique")
    op.drop_index("idx_sn_assets_sn", table_name="sn_assets")
    op.create_unique_constraint("uk_sn_assets_sn", "sn_assets", ["sn"])
