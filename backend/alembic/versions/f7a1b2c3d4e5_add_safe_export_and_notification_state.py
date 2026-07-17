"""add safe export, relay tracking, and per-user notification state

Revision ID: f7a1b2c3d4e5
Revises: e6f0a1b2c3d4
Create Date: 2026-07-16 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision = "f7a1b2c3d4e5"
down_revision = "e6f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repair_tickets", sa.Column("language_code", sa.String(length=20), server_default="unknown", nullable=False))
    op.add_column("repair_tickets", sa.Column("rma_required", mysql.TINYINT(display_width=1), server_default=sa.text("0"), nullable=False))
    op.add_column("repair_tickets", sa.Column("relay_export_status", sa.String(length=30), server_default="not_required", nullable=False))
    op.add_column("repair_tickets", sa.Column("rma_status", sa.String(length=30), server_default="not_required", nullable=False))
    op.add_column("repair_tickets", sa.Column("sn_validation_status", sa.String(length=30), server_default="pending", nullable=False))
    op.add_column("repair_tickets", sa.Column("sn_validation_snapshot", mysql.JSON(), nullable=True))
    op.add_column("repair_tickets", sa.Column("sn_validation_hash", mysql.CHAR(length=64), nullable=True))
    op.add_column("repair_tickets", sa.Column("sn_validated_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("repair_tickets", sa.Column("safety_check_snapshot", mysql.JSON(), nullable=True))
    op.add_column("repair_tickets", sa.Column("safety_check_hash", mysql.CHAR(length=64), nullable=True))
    op.add_column("repair_tickets", sa.Column("safety_checked_at", mysql.DATETIME(fsp=3), nullable=True))
    op.create_index("idx_repair_tickets_relay_status", "repair_tickets", ["relay_export_status", "updated_at"])
    op.create_index("idx_repair_tickets_rma_status", "repair_tickets", ["rma_status", "updated_at"])
    op.create_index("idx_repair_tickets_sn_validation_status", "repair_tickets", ["sn_validation_status", "updated_at"])

    op.add_column("sn_validation_results", sa.Column("ticket_version", sa.Integer(), server_default="1", nullable=False))
    op.add_column("sn_validation_results", sa.Column("input_hash", mysql.CHAR(length=64), nullable=True))
    op.add_column("sn_validation_results", sa.Column("source_system", sa.String(length=30), server_default="local_sn_assets", nullable=False))
    op.add_column("sn_validation_results", sa.Column("evidence_json", mysql.JSON(), nullable=True))

    op.add_column("sn_assets", sa.Column("source_system", sa.String(length=30), server_default="local", nullable=False))
    op.add_column("sn_assets", sa.Column("external_id", sa.String(length=191), nullable=True))
    op.add_column("sn_assets", sa.Column("source_updated_at", mysql.DATETIME(fsp=3), nullable=True))
    op.create_index("idx_sn_assets_external", "sn_assets", ["source_system", "external_id"])
    op.create_index("idx_sn_assets_source_updated", "sn_assets", ["source_system", "source_updated_at"])

    op.create_table(
        "external_sync_checkpoints",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("sync_name", sa.String(length=100), nullable=False),
        sa.Column("cursor_value", sa.String(length=500), nullable=True),
        sa.Column("last_full_sync_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("last_success_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("last_status", sa.String(length=30), server_default="never_run", nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("statistics_json", mysql.JSON(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sync_name", name="uk_external_sync_checkpoints_name"),
    )
    op.create_table(
        "ticket_relay_exports",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("ticket_version", sa.Integer(), nullable=False),
        sa.Column("payload_hash", mysql.CHAR(length=64), nullable=False),
        sa.Column("payload_snapshot", mysql.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("remote_record_key", sa.String(length=191), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("next_retry_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("exported_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["repair_tickets.id"], name="fk_ticket_relay_export_ticket", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_id", "ticket_version", "payload_hash", name="uk_ticket_relay_export_snapshot"),
    )
    op.create_index("idx_ticket_relay_export_status", "ticket_relay_exports", ["status", "next_retry_at"])
    op.create_index("idx_ticket_relay_export_ticket", "ticket_relay_exports", ["ticket_id", "created_at"])

    op.create_table(
        "notification_user_states",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("notification_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="unread", nullable=False),
        sa.Column("read_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("resolved_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(["notification_id"], ["notification_events.id"], name="fk_notification_user_state_event", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_notification_user_state_user", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_id", "user_id", name="uk_notification_user_state"),
    )
    op.create_index("idx_notification_user_unread", "notification_user_states", ["user_id", "status", "updated_at"])

    # Existing global/role/user events become per-user states. Resolved business
    # tasks are repaired without relying on a hard-coded ticket or notification id.
    op.execute(sa.text("""
        INSERT IGNORE INTO notification_user_states
            (notification_id, user_id, status, read_at, resolved_at, created_at, updated_at)
        SELECT n.id, u.id,
               CASE
                 WHEN n.delivery_status = 'read' THEN 'read'
                 WHEN n.target_type = 'manual_review_task' AND mrt.status IN ('resolved', 'closed') THEN 'resolved'
                 WHEN n.target_type = 'manual_review_task' AND rt.current_status_code <> 'manual_review' THEN 'resolved'
                 ELSE 'unread'
               END,
               n.read_at,
               CASE
                 WHEN n.target_type = 'manual_review_task' AND (mrt.status IN ('resolved', 'closed') OR rt.current_status_code <> 'manual_review')
                 THEN CURRENT_TIMESTAMP(3) ELSE NULL
               END,
               n.created_at, CURRENT_TIMESTAMP(3)
        FROM notification_events n
        JOIN users u ON u.status = 'active'
        LEFT JOIN user_roles ur ON ur.user_id = u.id
        LEFT JOIN roles r ON r.id = ur.role_id
        LEFT JOIN manual_review_tasks mrt ON n.target_type = 'manual_review_task' AND mrt.id = n.target_id
        LEFT JOIN repair_tickets rt ON rt.id = mrt.ticket_id
        WHERE (n.recipient_user_id IS NULL OR n.recipient_user_id = u.id)
          AND (n.recipient_role_code IS NULL OR n.recipient_role_code = r.role_code)
    """))
    op.execute(sa.text("""
        UPDATE manual_review_tasks mrt
        JOIN repair_tickets rt ON rt.id = mrt.ticket_id
        SET mrt.status = 'closed',
            mrt.resolution = COALESCE(mrt.resolution, 'Automatically closed after ticket left manual review'),
            mrt.resolved_at = COALESCE(mrt.resolved_at, CURRENT_TIMESTAMP(3)),
            mrt.updated_at = CURRENT_TIMESTAMP(3)
        WHERE mrt.status NOT IN ('resolved', 'closed')
          AND rt.current_status_code <> 'manual_review'
    """))


def downgrade() -> None:
    op.drop_index("idx_notification_user_unread", table_name="notification_user_states")
    op.drop_table("notification_user_states")
    op.drop_index("idx_ticket_relay_export_ticket", table_name="ticket_relay_exports")
    op.drop_index("idx_ticket_relay_export_status", table_name="ticket_relay_exports")
    op.drop_table("ticket_relay_exports")
    op.drop_table("external_sync_checkpoints")
    op.drop_index("idx_sn_assets_source_updated", table_name="sn_assets")
    op.drop_index("idx_sn_assets_external", table_name="sn_assets")
    op.drop_column("sn_assets", "source_updated_at")
    op.drop_column("sn_assets", "external_id")
    op.drop_column("sn_assets", "source_system")
    op.drop_column("sn_validation_results", "evidence_json")
    op.drop_column("sn_validation_results", "source_system")
    op.drop_column("sn_validation_results", "input_hash")
    op.drop_column("sn_validation_results", "ticket_version")
    op.drop_index("idx_repair_tickets_sn_validation_status", table_name="repair_tickets")
    op.drop_column("repair_tickets", "sn_validated_at")
    op.drop_column("repair_tickets", "sn_validation_hash")
    op.drop_column("repair_tickets", "sn_validation_snapshot")
    op.drop_column("repair_tickets", "sn_validation_status")
    op.drop_index("idx_repair_tickets_rma_status", table_name="repair_tickets")
    op.drop_index("idx_repair_tickets_relay_status", table_name="repair_tickets")
    op.drop_column("repair_tickets", "safety_checked_at")
    op.drop_column("repair_tickets", "safety_check_hash")
    op.drop_column("repair_tickets", "safety_check_snapshot")
    op.drop_column("repair_tickets", "rma_status")
    op.drop_column("repair_tickets", "relay_export_status")
    op.drop_column("repair_tickets", "rma_required")
    op.drop_column("repair_tickets", "language_code")
