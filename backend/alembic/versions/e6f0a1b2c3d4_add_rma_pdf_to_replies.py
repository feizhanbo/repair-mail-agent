"""associate generated RMA authorization PDFs with replies

Revision ID: e6f0a1b2c3d4
Revises: d5e9f7a2b4c6
Create Date: 2026-07-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision = "e6f0a1b2c3d4"
down_revision = "d5e9f7a2b4c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reply_records", sa.Column("rma_pdf_oss_object_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("reply_records", sa.Column("reply_template_version", sa.String(length=100), nullable=True))
    op.add_column("reply_records", sa.Column("rma_template_version", sa.String(length=100), nullable=True))
    op.add_column("reply_records", sa.Column("rma_pdf_data_snapshot", mysql.JSON(), nullable=True))
    op.create_index("idx_reply_records_rma_pdf", "reply_records", ["rma_pdf_oss_object_id"])
    op.create_foreign_key(
        "fk_reply_records_rma_pdf", "reply_records", "oss_objects",
        ["rma_pdf_oss_object_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_reply_records_rma_pdf", "reply_records", type_="foreignkey")
    op.drop_index("idx_reply_records_rma_pdf", table_name="reply_records")
    op.drop_column("reply_records", "rma_pdf_data_snapshot")
    op.drop_column("reply_records", "rma_template_version")
    op.drop_column("reply_records", "reply_template_version")
    op.drop_column("reply_records", "rma_pdf_oss_object_id")
