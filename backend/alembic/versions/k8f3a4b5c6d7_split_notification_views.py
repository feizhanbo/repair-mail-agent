"""add notification-center classification and ticket grouping

Revision ID: k8f3a4b5c6d7
Revises: j7e1f2a3b4c5
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "k8f3a4b5c6d7"
down_revision: str | None = "j7e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ATTENTION_EVENTS = (
    "manual_review_created",
    "manual_review_assigned",
    "manual_review_assignment_failed",
    "manual_review_owner_corrected",
    "ticket_customer_info_required",
    "ticket_system_error",
    "sap_export_failed",
    "sap_submit_uncertain",
    "rma_reply_failed",
)


def upgrade() -> None:
    op.add_column(
        "notification_events",
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.add_column(
        "notification_events",
        sa.Column("requires_attention", mysql.TINYINT(display_width=1), server_default=sa.text("0"), nullable=False),
    )
    op.create_foreign_key(
        "fk_notifications_ticket",
        "notification_events",
        "repair_tickets",
        ["ticket_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_notifications_ticket_attention",
        "notification_events",
        ["ticket_id", "requires_attention", "created_at"],
    )

    op.execute(
        sa.text(
            """
            UPDATE notification_events
            SET ticket_id = target_id
            WHERE target_type IN ('repair_ticket', 'ticket')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE notification_events n
            JOIN manual_review_tasks mrt ON n.target_type = 'manual_review_task' AND n.target_id = mrt.id
            SET n.ticket_id = mrt.ticket_id
            WHERE n.ticket_id IS NULL
            """
        )
    )
    placeholders = ", ".join(f":event_{index}" for index, _ in enumerate(ATTENTION_EVENTS))
    op.execute(
        sa.text(f"UPDATE notification_events SET requires_attention = 1 WHERE event_type IN ({placeholders})").bindparams(
            **{f"event_{index}": value for index, value in enumerate(ATTENTION_EVENTS)}
        )
    )

    # Preserve history, but close stale actionable events that were superseded
    # by a later successful operation or by the current ticket state.
    op.execute(
        sa.text(
            """
            UPDATE notification_user_states s
            JOIN notification_events e ON e.id = s.notification_id
            JOIN notification_events success
              ON success.ticket_id = e.ticket_id
             AND success.id > e.id
             AND success.event_type IN ('sap_export_accepted', 'rma_reply_sent')
            SET s.status = 'resolved',
                s.read_at = COALESCE(s.read_at, CURRENT_TIMESTAMP(3)),
                s.resolved_at = COALESCE(s.resolved_at, success.created_at),
                s.updated_at = CURRENT_TIMESTAMP(3)
            WHERE s.status <> 'resolved'
              AND e.event_type IN ('sap_export_failed', 'sap_submit_uncertain', 'rma_reply_failed')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE notification_user_states s
            JOIN notification_events e ON e.id = s.notification_id
            JOIN repair_tickets t ON t.id = e.ticket_id
            SET s.status = 'resolved',
                s.read_at = COALESCE(s.read_at, CURRENT_TIMESTAMP(3)),
                s.resolved_at = COALESCE(s.resolved_at, CURRENT_TIMESTAMP(3)),
                s.updated_at = CURRENT_TIMESTAMP(3)
            WHERE s.status <> 'resolved'
              AND e.event_type = 'ticket_customer_info_required'
              AND t.current_status_code NOT IN ('need_customer_info', 'auto_replied')
            """
        )
    )


def downgrade() -> None:
    op.drop_index("idx_notifications_ticket_attention", table_name="notification_events")
    op.drop_constraint("fk_notifications_ticket", "notification_events", type_="foreignkey")
    op.drop_column("notification_events", "requires_attention")
    op.drop_column("notification_events", "ticket_id")
