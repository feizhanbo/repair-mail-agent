"""add company device receipt audit state

Revision ID: b9c3d4e5f6a7
Revises: a8b2c3d4e5f6
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "b9c3d4e5f6a7"
down_revision = "a8b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repair_tickets", sa.Column("device_received_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("repair_tickets", sa.Column("device_received_source", sa.String(length=30), nullable=True))
    op.add_column("repair_tickets", sa.Column("device_received_email_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("repair_tickets", sa.Column("device_received_note", sa.Text(), nullable=True))
    op.add_column("repair_tickets", sa.Column("device_received_idempotency_key", sa.String(length=100), nullable=True))
    op.add_column(
        "repair_tickets",
        sa.Column("device_receipt_ack_status", sa.String(length=30), server_default="not_received", nullable=False),
    )
    op.create_foreign_key(
        "fk_repair_tickets_device_received_email",
        "repair_tickets",
        "emails",
        ["device_received_email_id"],
        ["id"],
    )
    op.create_index(
        "idx_repair_tickets_device_ack_status",
        "repair_tickets",
        ["device_receipt_ack_status", "updated_at"],
        unique=False,
    )
    op.execute(
        "UPDATE workflow_transitions "
        "SET trigger_event='device_receipt_ack_sent', "
        "condition_desc='Close only after the company receives the repair device and the receipt acknowledgement is sent.' "
        "WHERE from_status_code='ready_for_export' AND to_status_code='closed' "
        "AND trigger_event='customer_receipt_confirmed'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE workflow_transitions "
        "SET trigger_event='customer_receipt_confirmed', "
        "condition_desc='Legacy: close after customer confirms receipt of the repaired device.' "
        "WHERE from_status_code='ready_for_export' AND to_status_code='closed' "
        "AND trigger_event='device_receipt_ack_sent'"
    )
    op.drop_index("idx_repair_tickets_device_ack_status", table_name="repair_tickets")
    op.drop_constraint("fk_repair_tickets_device_received_email", "repair_tickets", type_="foreignkey")
    op.drop_column("repair_tickets", "device_receipt_ack_status")
    op.drop_column("repair_tickets", "device_received_idempotency_key")
    op.drop_column("repair_tickets", "device_received_note")
    op.drop_column("repair_tickets", "device_received_email_id")
    op.drop_column("repair_tickets", "device_received_source")
    op.drop_column("repair_tickets", "device_received_at")
