"""enforce device receipt acknowledgement as the only close event

Revision ID: c0d4e5f6a7b8
Revises: b9c3d4e5f6a7
"""

from alembic import op


revision = "c0d4e5f6a7b8"
down_revision = "b9c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE workflow_transitions SET enabled=0 "
        "WHERE from_status_code='ready_for_export' AND to_status_code='closed' "
        "AND trigger_event IN ('export_completed', 'manual_close', 'customer_receipt_confirmed')"
    )
    op.execute(
        "UPDATE workflow_transitions "
        "SET enabled=1, require_manual=0, "
        "condition_desc='Close only after the company receives the repair device and the receipt acknowledgement is sent.' "
        "WHERE from_status_code='ready_for_export' AND to_status_code='closed' "
        "AND trigger_event='device_receipt_ack_sent'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE workflow_transitions SET enabled=1 "
        "WHERE from_status_code='ready_for_export' AND to_status_code='closed' "
        "AND trigger_event IN ('export_completed', 'manual_close')"
    )
