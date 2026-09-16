"""Harden single-worker job scheduling and retry lineage.

Revision ID: d7y2z3a4b5c6
Revises: c6x1y2z3a4b5
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "d7y2z3a4b5c6"
down_revision: str | None = "c6x1y2z3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("idx_job_run_logs_queue", table_name="job_run_logs")
    op.add_column(
        "job_run_logs",
        sa.Column("priority", mysql.TINYINT(unsigned=True), server_default="2", nullable=False),
    )
    op.add_column(
        "job_run_logs",
        sa.Column("retry_of_job_id", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.add_column(
        "job_run_logs",
        sa.Column("execution_deadline_at", mysql.DATETIME(fsp=3), nullable=True),
    )
    op.create_foreign_key(
        "fk_job_run_logs_retry_of",
        "job_run_logs",
        "job_run_logs",
        ["retry_of_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uk_job_run_logs_retry_of", "job_run_logs", ["retry_of_job_id"]
    )
    op.create_check_constraint(
        op.f("ck_job_run_logs_priority"), "job_run_logs", "priority BETWEEN 0 AND 3"
    )
    op.create_index(
        "idx_job_run_logs_queue",
        "job_run_logs",
        ["status", "priority", "next_run_at", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_job_run_logs_queue", table_name="job_run_logs")
    op.drop_constraint(op.f("ck_job_run_logs_priority"), "job_run_logs", type_="check")
    op.drop_constraint("uk_job_run_logs_retry_of", "job_run_logs", type_="unique")
    op.drop_constraint("fk_job_run_logs_retry_of", "job_run_logs", type_="foreignkey")
    op.drop_column("job_run_logs", "execution_deadline_at")
    op.drop_column("job_run_logs", "retry_of_job_id")
    op.drop_column("job_run_logs", "priority")
    op.create_index(
        "idx_job_run_logs_queue",
        "job_run_logs",
        ["status", "next_run_at", "created_at"],
        unique=False,
    )
