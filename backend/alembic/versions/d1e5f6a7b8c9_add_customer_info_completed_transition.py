"""allow completed required fields to resume validation

Revision ID: d1e5f6a7b8c9
Revises: c0d4e5f6a7b8
"""

from alembic import op


revision = "d1e5f6a7b8c9"
down_revision = "c0d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO workflow_transitions "
        "(from_status_code, to_status_code, trigger_event, condition_desc, require_manual, enabled, created_at, updated_at) "
        "SELECT 'need_customer_info', 'parsed', 'customer_info_completed', "
        "'Required customer fields were completed during a deterministic reparse.', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM workflow_transitions "
        "WHERE from_status_code='need_customer_info' AND to_status_code='parsed' "
        "AND trigger_event='customer_info_completed'"
        ")"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM workflow_transitions "
        "WHERE from_status_code='need_customer_info' AND to_status_code='parsed' "
        "AND trigger_event='customer_info_completed'"
    )
