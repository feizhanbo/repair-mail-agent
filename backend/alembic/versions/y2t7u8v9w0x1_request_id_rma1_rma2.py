"""RequestID and SAP RMA1/RMA2 local contract.

Revision ID: y2t7u8v9w0x1
Revises: x1s6n7o8p9q0
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql


revision: str = "y2t7u8v9w0x1"
down_revision: str | None = "x1s6n7o8p9q0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not context.is_offline_mode():
        connection = op.get_bind()
        invalid = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM export_sap "
                "WHERE CHAR_LENGTH(source_request_id) <> 36 "
                "AND status NOT IN ('rma_received', 'manual_review', 'timed_out')"
            )
        ).scalar_one()
        if int(invalid or 0):
            raise RuntimeError("IN_FLIGHT_REQUEST_ID_NOT_CHAR36")

    op.drop_constraint("uk_export_sap_source_request_id", "export_sap", type_="unique")
    op.alter_column(
        "export_sap",
        "source_request_id",
        existing_type=sa.String(length=64),
        new_column_name="RequestID",
        type_=mysql.CHAR(length=36),
        existing_nullable=False,
    )
    op.create_unique_constraint("uk_export_sap_request_id", "export_sap", ["RequestID"])
    op.add_column("sn_assets", sa.Column("ins_id", sa.Integer(), nullable=True))
    op.add_column("sn_assets", sa.Column("source_row_hash", mysql.CHAR(length=64), nullable=True))
    op.create_index("idx_sn_assets_ins_id", "sn_assets", ["ins_id"])
    op.alter_column(
        "sn_assets",
        "customer_name",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "sap_sn_staging",
        "customer_name",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "sap_sn_staging",
        "customer_name",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "sn_assets",
        "customer_name",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_index("idx_sn_assets_ins_id", table_name="sn_assets")
    op.drop_column("sn_assets", "source_row_hash")
    op.drop_column("sn_assets", "ins_id")
    op.drop_constraint("uk_export_sap_request_id", "export_sap", type_="unique")
    op.alter_column(
        "export_sap",
        "RequestID",
        existing_type=mysql.CHAR(length=36),
        new_column_name="source_request_id",
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_unique_constraint(
        "uk_export_sap_source_request_id", "export_sap", ["source_request_id"]
    )
