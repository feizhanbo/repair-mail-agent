"""keep ticket at rma_sent after RMA issue archival

Revision ID: r5m0h1c2d3e4
Revises: q4l9g0b1c2d3
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op


revision: str = "r5m0h1c2d3e4"
down_revision: str | None = "q4l9g0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE workflow_transitions SET enabled=0 "
        "WHERE to_status_code='closed'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE workflow_transitions SET enabled=1, require_manual=0 "
        "WHERE from_status_code='rma_sent' AND to_status_code='closed' "
        "AND trigger_event='rma_issued_and_archived'"
    )
