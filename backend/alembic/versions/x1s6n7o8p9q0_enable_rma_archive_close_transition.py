"""enable the evidence-gated RMA archive close transition

Revision ID: x1s6n7o8p9q0
Revises: w0r5m6n7o8p9
"""

from alembic import op


revision = "x1s6n7o8p9q0"
down_revision = "w0r5m6n7o8p9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE workflow_transitions SET enabled=1 "
        "WHERE from_status_code='rma_sent' "
        "AND to_status_code='closed' "
        "AND trigger_event='rma_issued_and_archived'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE workflow_transitions SET enabled=0 "
        "WHERE from_status_code='rma_sent' "
        "AND to_status_code='closed' "
        "AND trigger_event='rma_issued_and_archived'"
    )
