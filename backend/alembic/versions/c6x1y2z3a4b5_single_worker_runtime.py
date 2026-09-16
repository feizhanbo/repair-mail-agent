"""add single worker lease and job fencing token

Revision ID: c6x1y2z3a4b5
Revises: b5w0x1y2z3a4
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "c6x1y2z3a4b5"
down_revision: str | None = "b5w0x1y2z3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_run_logs",
        sa.Column("fencing_token", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.create_table(
        "worker_leases",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("environment", sa.String(length=50), nullable=False),
        sa.Column("queue_name", sa.String(length=50), nullable=False),
        sa.Column("instance_id", sa.String(length=100), nullable=False),
        sa.Column("app_version", sa.String(length=100), nullable=False),
        sa.Column(
            "fencing_token",
            mysql.BIGINT(unsigned=True),
            server_default="1",
            nullable=False,
        ),
        sa.Column("heartbeat_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.Column("lease_expires_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("environment", "queue_name", name="uk_worker_leases_scope"),
    )
    op.create_index("idx_worker_leases_expiry", "worker_leases", ["lease_expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_worker_leases_expiry", table_name="worker_leases")
    op.drop_table("worker_leases")
    op.drop_column("job_run_logs", "fencing_token")
