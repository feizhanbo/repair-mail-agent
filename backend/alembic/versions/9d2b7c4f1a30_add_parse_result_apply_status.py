"""add parse result apply status

Revision ID: 9d2b7c4f1a30
Revises: 0f2ae6ba263f
Create Date: 2026-07-05 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "9d2b7c4f1a30"
down_revision = "0f2ae6ba263f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "parse_results",
        sa.Column("apply_status", sa.String(length=30), server_default="pending", nullable=False),
    )
    op.add_column("parse_results", sa.Column("applied_by_user_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("parse_results", sa.Column("applied_at", mysql.DATETIME(fsp=3), nullable=True))
    op.create_foreign_key(
        "fk_parse_results_applied_by",
        "parse_results",
        "users",
        ["applied_by_user_id"],
        ["id"],
    )
    op.create_index("idx_parse_results_apply_status", "parse_results", ["apply_status"], unique=False)
    op.execute(
        """
        UPDATE parse_results
        SET apply_status = CASE
            WHEN accepted = 1 AND accepted_by_user_id IS NULL THEN 'auto_applied'
            WHEN accepted = 1 AND accepted_by_user_id IS NOT NULL THEN 'manually_applied'
            ELSE 'pending'
        END,
        applied_by_user_id = accepted_by_user_id,
        applied_at = accepted_at
        """
    )


def downgrade() -> None:
    op.drop_index("idx_parse_results_apply_status", table_name="parse_results")
    op.drop_constraint("fk_parse_results_applied_by", "parse_results", type_="foreignkey")
    op.drop_column("parse_results", "applied_at")
    op.drop_column("parse_results", "applied_by_user_id")
    op.drop_column("parse_results", "apply_status")
