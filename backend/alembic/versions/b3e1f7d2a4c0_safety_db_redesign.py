"""safety db redesign

Revision ID: b3e1f7d2a4c0
Revises: 9d2b7c4f1a30
Create Date: 2026-07-11 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "b3e1f7d2a4c0"
down_revision = "9d2b7c4f1a30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uk_emails_message_id_hash", "emails", type_="unique")
    op.drop_column("emails", "message_id_hash")
    op.create_unique_constraint("uk_emails_message_id", "emails", ["message_id"])

    op.create_table(
        "mail_fetch_records",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("mailbox_account", sa.String(length=255), nullable=False),
        sa.Column("folder_name", sa.String(length=255), nullable=False),
        sa.Column("imap_uid", sa.String(length=100), nullable=False),
        sa.Column("message_id", sa.String(length=500), nullable=False),
        sa.Column("fetch_job_run_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("email_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("duplicate", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("fetch_status", sa.String(length=30), server_default="success", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], name="fk_mail_fetch_records_email"),
        sa.ForeignKeyConstraint(["fetch_job_run_id"], ["job_run_logs.id"], name="fk_mail_fetch_records_job"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_fetch_records")),
        sa.UniqueConstraint("mailbox_account", "folder_name", "imap_uid", name="uk_mail_fetch_records"),
    )
    op.create_index("idx_mail_fetch_records_message_id", "mail_fetch_records", ["message_id"], unique=False)
    op.create_index("idx_mail_fetch_records_job", "mail_fetch_records", ["fetch_job_run_id"], unique=False)

    op.execute("""
        CREATE OR REPLACE VIEW business_emails AS
        SELECT id, thread_id, message_id, mail_direction, from_address, from_domain,
               to_addresses, subject, normalized_subject, sent_at, received_at,
               parse_status, intent_type, duplicate_of_email_id,
               CASE WHEN mail_direction = 'inbound' THEN 'inbound' ELSE 'outbound' END AS email_category,
               created_at, updated_at
        FROM emails;
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS business_emails")

    op.drop_index("idx_mail_fetch_records_job", table_name="mail_fetch_records")
    op.drop_index("idx_mail_fetch_records_message_id", table_name="mail_fetch_records")
    op.drop_table("mail_fetch_records")

    op.drop_constraint("uk_emails_message_id", "emails", type_="unique")
    op.add_column("emails", sa.Column("message_id_hash", mysql.CHAR(length=64), nullable=False, server_default=""))
    op.create_unique_constraint("uk_emails_message_id_hash", "emails", ["message_id_hash"])
