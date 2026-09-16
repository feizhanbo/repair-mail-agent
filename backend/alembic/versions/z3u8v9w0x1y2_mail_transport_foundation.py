"""Add mailbox sync, immutable outbox and delivery event foundations.

Revision ID: z3u8v9w0x1y2
Revises: y2t7u8v9w0x1
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "z3u8v9w0x1y2"
down_revision: str | None = "y2t7u8v9w0x1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mailbox_sync_states",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("mailbox_account", sa.String(length=255), nullable=False),
        sa.Column("folder_name", sa.String(length=255), nullable=False),
        sa.Column("uid_validity", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("sync_mode", sa.String(length=30), server_default="paused", nullable=False),
        sa.Column("initial_sync_start_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("initial_sync_completed_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("last_discovered_uid", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("last_fetched_uid", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("last_sync_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("last_success_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_mailbox_sync_states"),
        sa.UniqueConstraint("mailbox_account", "folder_name", name="uk_mailbox_sync_account_folder"),
    )
    op.create_index("idx_mailbox_sync_mode_lease", "mailbox_sync_states", ["sync_mode", "lease_expires_at"])

    op.alter_column(
        "mail_fetch_records",
        "message_id",
        existing_type=sa.String(length=500),
        nullable=True,
    )
    op.add_column("mail_fetch_records", sa.Column("raw_eml_oss_object_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("mail_fetch_records", sa.Column("raw_eml_sha256", mysql.CHAR(length=64), nullable=True))
    op.add_column("mail_fetch_records", sa.Column("internal_date", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column(
        "mail_fetch_records",
        sa.Column("raw_retention_mode", sa.String(length=30), server_default="temporary", nullable=False),
    )
    op.create_foreign_key(
        "fk_mail_fetch_records_raw_eml_oss",
        "mail_fetch_records",
        "oss_objects",
        ["raw_eml_oss_object_id"],
        ["id"],
    )
    op.create_index("idx_mail_fetch_records_raw_hash", "mail_fetch_records", ["raw_eml_sha256"])

    op.add_column("email_attachments", sa.Column("original_content_type", sa.String(length=255), nullable=True))
    op.add_column("email_attachments", sa.Column("detected_content_type", sa.String(length=255), nullable=True))
    op.add_column("email_attachments", sa.Column("content_disposition", sa.String(length=30), nullable=True))
    op.add_column(
        "email_attachments",
        sa.Column("resource_role", sa.String(length=30), server_default="regular_attachment", nullable=False),
    )
    op.add_column("email_attachments", sa.Column("file_extension", sa.String(length=30), nullable=True))

    op.create_table(
        "email_outbox",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("reply_record_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("related_email_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("frozen_eml_oss_object_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=191), nullable=False),
        sa.Column("message_id", sa.String(length=500), nullable=False),
        sa.Column("from_address", sa.String(length=500), nullable=False),
        sa.Column("to_addresses", sa.Text(), nullable=False),
        sa.Column("cc_addresses", sa.Text(), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("frozen_eml_sha256", mysql.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="preparing", nullable=False),
        sa.Column("ticket_version", sa.Integer(), nullable=True),
        sa.Column("thread_history_hash", mysql.CHAR(length=64), nullable=True),
        sa.Column("request_id", mysql.CHAR(length=36), nullable=True),
        sa.Column("rma_no", sa.String(length=100), nullable=True),
        sa.Column("template_version", sa.String(length=100), nullable=True),
        sa.Column("pdf_sha256", mysql.CHAR(length=64), nullable=True),
        sa.Column("safety_snapshot", mysql.JSON(), nullable=True),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("smtp_response", sa.String(length=1000), nullable=True),
        sa.Column("accepted_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(["frozen_eml_oss_object_id"], ["oss_objects.id"], name="fk_email_outbox_frozen_eml_oss"),
        sa.ForeignKeyConstraint(["related_email_id"], ["emails.id"], name="fk_email_outbox_related_email", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reply_record_id"], ["reply_records.id"], name="fk_email_outbox_reply", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ticket_id"], ["repair_tickets.id"], name="fk_email_outbox_ticket", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_email_outbox"),
        sa.UniqueConstraint("idempotency_key", name="uk_email_outbox_idempotency"),
        sa.UniqueConstraint("message_id", name="uk_email_outbox_message_id"),
        sa.UniqueConstraint("reply_record_id", name="uk_email_outbox_reply"),
    )
    op.create_index("idx_email_outbox_claim", "email_outbox", ["status", "next_attempt_at", "lease_expires_at"])
    op.create_index("idx_email_outbox_ticket", "email_outbox", ["ticket_id", "created_at"])

    op.create_table(
        "mail_delivery_events",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("event_key", sa.String(length=191), nullable=False),
        sa.Column("outbox_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("source_email_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("original_message_id", sa.String(length=500), nullable=True),
        sa.Column("final_recipient", sa.String(length=500), nullable=True),
        sa.Column("delivery_status", sa.String(length=30), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=True),
        sa.Column("smtp_status_code", sa.String(length=30), nullable=True),
        sa.Column("diagnostic_code", sa.String(length=1000), nullable=True),
        sa.Column("evidence", mysql.JSON(), nullable=True),
        sa.Column("occurred_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(["outbox_id"], ["email_outbox.id"], name="fk_mail_delivery_event_outbox", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_email_id"], ["emails.id"], name="fk_mail_delivery_event_source_email", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["ticket_id"], ["repair_tickets.id"], name="fk_mail_delivery_event_ticket", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_mail_delivery_events"),
        sa.UniqueConstraint("event_key", name="uk_mail_delivery_event_key"),
    )
    op.create_index("idx_mail_delivery_outbox", "mail_delivery_events", ["outbox_id", "created_at"])
    op.create_index("idx_mail_delivery_status", "mail_delivery_events", ["delivery_status", "created_at"])
    op.create_index("idx_mail_delivery_ticket", "mail_delivery_events", ["ticket_id", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_mail_delivery_ticket", table_name="mail_delivery_events")
    op.drop_index("idx_mail_delivery_status", table_name="mail_delivery_events")
    op.drop_index("idx_mail_delivery_outbox", table_name="mail_delivery_events")
    op.drop_table("mail_delivery_events")
    op.drop_index("idx_email_outbox_ticket", table_name="email_outbox")
    op.drop_index("idx_email_outbox_claim", table_name="email_outbox")
    op.drop_table("email_outbox")
    op.drop_column("email_attachments", "file_extension")
    op.drop_column("email_attachments", "resource_role")
    op.drop_column("email_attachments", "content_disposition")
    op.drop_column("email_attachments", "detected_content_type")
    op.drop_column("email_attachments", "original_content_type")
    op.drop_index("idx_mail_fetch_records_raw_hash", table_name="mail_fetch_records")
    op.drop_constraint("fk_mail_fetch_records_raw_eml_oss", "mail_fetch_records", type_="foreignkey")
    op.drop_column("mail_fetch_records", "raw_retention_mode")
    op.drop_column("mail_fetch_records", "internal_date")
    op.drop_column("mail_fetch_records", "raw_eml_sha256")
    op.drop_column("mail_fetch_records", "raw_eml_oss_object_id")
    op.alter_column(
        "mail_fetch_records",
        "message_id",
        existing_type=sa.String(length=500),
        nullable=False,
    )
    op.drop_index("idx_mailbox_sync_mode_lease", table_name="mailbox_sync_states")
    op.drop_table("mailbox_sync_states")
