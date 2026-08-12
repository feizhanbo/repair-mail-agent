"""source request id SAP adapter and SN snapshot staging

Revision ID: q4l9g0b1c2d3
Revises: p3k8f9a0b1c2
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql


revision: str = "q4l9g0b1c2d3"
down_revision: str | None = "p3k8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if not context.is_offline_mode():
        in_flight = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM export_sap "
                "WHERE remote_call_id IS NOT NULL AND status NOT IN ('rma_received', 'manual_review', 'timed_out')"
            )
        ).scalar_one()
        if int(in_flight or 0) > 0:
            raise RuntimeError("LEGACY_CALL_ID_EXPORTS_STILL_IN_FLIGHT")

    op.drop_constraint("uk_export_sap_submission_key", "export_sap", type_="unique")
    op.alter_column(
        "export_sap",
        "submission_key",
        existing_type=sa.String(length=64),
        new_column_name="source_request_id",
        existing_nullable=False,
    )
    op.create_unique_constraint(
        "uk_export_sap_source_request_id", "export_sap", ["source_request_id"]
    )

    op.drop_constraint("uk_ticket_rmas_no", "ticket_rmas", type_="unique")
    op.add_column("ticket_rmas", sa.Column("customer_code", sa.String(length=50), nullable=True))
    op.add_column("ticket_rmas", sa.Column("repair_business_date", sa.Date(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE ticket_rmas tr "
            "LEFT JOIN ("
            " SELECT tri.ticket_rma_id, MIN(es.customer_code) customer_code, "
            "        DATE(MIN(es.repair_requested_at)) repair_business_date "
            " FROM ticket_rma_items tri "
            " JOIN export_sap es ON es.ticket_item_id = tri.ticket_item_id "
            " GROUP BY tri.ticket_rma_id"
            ") src ON src.ticket_rma_id = tr.id "
            "SET tr.customer_code = src.customer_code, "
            "    tr.repair_business_date = src.repair_business_date"
        )
    )
    op.create_unique_constraint("uk_ticket_rmas_ticket_no", "ticket_rmas", ["ticket_id", "rma_no"])
    op.create_index(
        "idx_ticket_rmas_business_identity",
        "ticket_rmas",
        ["rma_no", "customer_code", "repair_business_date"],
    )

    op.create_table(
        "sap_sn_sync_batches",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("batch_no", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("source_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("valid_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("invalid_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicate_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("previous_count", sa.Integer(), nullable=True),
        sa.Column("count_change_percent", sa.Numeric(10, 4), nullable=True),
        sa.Column("snapshot_hash", mysql.CHAR(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("approval_reason", sa.String(length=500), nullable=True),
        sa.Column("approved_by_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("started_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("finished_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("applied_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"], ["users.id"], name="fk_sap_sn_sync_batches_approved_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_no", name="uk_sap_sn_sync_batches_no"),
    )
    op.create_index(
        "idx_sap_sn_sync_batches_status", "sap_sn_sync_batches", ["status", "created_at"]
    )
    op.create_table(
        "sap_sn_staging",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("sync_batch_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("sn", sa.String(length=100), nullable=False),
        sa.Column("customer_code", sa.String(length=50), nullable=False),
        sa.Column("customer_name", sa.String(length=255), nullable=False),
        sa.Column("material_code", sa.String(length=100), nullable=False),
        sa.Column("material_name", sa.String(length=255), nullable=True),
        sa.Column("asset_status", sa.String(length=30), server_default="valid", nullable=False),
        sa.Column("values_json", mysql.JSON(), nullable=True),
        sa.Column("raw_data", mysql.JSON(), nullable=True),
        sa.Column("row_hash", mysql.CHAR(length=64), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(
            ["sync_batch_id"],
            ["sap_sn_sync_batches.id"],
            name="fk_sap_sn_staging_batch",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sync_batch_id", "sn", name="uk_sap_sn_staging_batch_sn"),
    )
    op.create_index("idx_sap_sn_staging_batch", "sap_sn_staging", ["sync_batch_id", "id"])


def downgrade() -> None:
    op.drop_index("idx_sap_sn_staging_batch", table_name="sap_sn_staging")
    op.drop_table("sap_sn_staging")
    op.drop_index("idx_sap_sn_sync_batches_status", table_name="sap_sn_sync_batches")
    op.drop_table("sap_sn_sync_batches")
    op.drop_index("idx_ticket_rmas_business_identity", table_name="ticket_rmas")
    op.drop_constraint("uk_ticket_rmas_ticket_no", "ticket_rmas", type_="unique")
    op.drop_column("ticket_rmas", "repair_business_date")
    op.drop_column("ticket_rmas", "customer_code")
    op.create_unique_constraint("uk_ticket_rmas_no", "ticket_rmas", ["rma_no"])
    op.drop_constraint("uk_export_sap_source_request_id", "export_sap", type_="unique")
    op.alter_column(
        "export_sap",
        "source_request_id",
        existing_type=sa.String(length=64),
        new_column_name="submission_key",
        existing_nullable=False,
    )
    op.create_unique_constraint("uk_export_sap_submission_key", "export_sap", ["submission_key"])
