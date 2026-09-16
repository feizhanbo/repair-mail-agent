"""Make updated_at advance on every row update.

Revision ID: e8z3a4b5c6d7
Revises: d7y2z3a4b5c6
"""

from __future__ import annotations

from alembic import op


revision: str = "e8z3a4b5c6d7"
down_revision: str | None = "d7y2z3a4b5c6"
branch_labels = None
depends_on = None


UPDATED_AT_TABLES = (
    "email_threads",
    "external_sync_checkpoints",
    "mailbox_sync_states",
    "roles",
    "users",
    "worker_leases",
    "workflow_statuses",
    "board_cards",
    "customer_service_policies",
    "reply_templates",
    "sap_sn_sync_batches",
    "sn_assets",
    "system_configs",
    "workflow_transitions",
    "job_run_logs",
    "repair_tickets",
    "sap_sn_staging",
    "emails",
    "repair_ticket_items",
    "ticket_relay_exports",
    "export_sap",
    "manual_review_tasks",
    "notification_user_states",
    "reply_records",
    "email_outbox",
    "external_operation_records",
    "ticket_rmas",
    "mail_delivery_events",
    "ticket_rma_items",
)


def upgrade() -> None:
    for table_name in UPDATED_AT_TABLES:
        op.execute(
            f"ALTER TABLE `{table_name}` MODIFY COLUMN `updated_at` "
            "DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) "
            "ON UPDATE CURRENT_TIMESTAMP(3)"
        )


def downgrade() -> None:
    for table_name in UPDATED_AT_TABLES:
        op.execute(
            f"ALTER TABLE `{table_name}` MODIFY COLUMN `updated_at` "
            "DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)"
        )
