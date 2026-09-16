"""add mail preclassification persistence state

Revision ID: v9q4l5m6n7o8
Revises: u8p3k4l5m6n7
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "v9q4l5m6n7o8"
down_revision = "u8p3k4l5m6n7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("emails", sa.Column("persistence_tier", sa.String(20), nullable=False, server_default="business"))
    op.add_column("emails", sa.Column("classification_locked", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.create_index("idx_emails_persistence_tier", "emails", ["persistence_tier"])

    op.add_column("mail_fetch_records", sa.Column("in_reply_to", sa.String(500)))
    op.add_column("mail_fetch_records", sa.Column("references_header", sa.Text()))
    op.add_column("mail_fetch_records", sa.Column("thread_id", mysql.BIGINT(unsigned=True)))
    op.add_column("mail_fetch_records", sa.Column("processing_stage", sa.String(50), nullable=False, server_default="discovered"))
    op.add_column("mail_fetch_records", sa.Column("recovery_stage", sa.String(50)))
    op.add_column("mail_fetch_records", sa.Column("intent_type", sa.String(50)))
    op.add_column("mail_fetch_records", sa.Column("handling_level", sa.String(30)))
    op.add_column("mail_fetch_records", sa.Column("classification_version", sa.String(50)))
    op.add_column("mail_fetch_records", sa.Column("classification_confidence", mysql.DECIMAL(5, 4)))
    op.add_column("mail_fetch_records", sa.Column("classification_reason_code", sa.String(100)))
    op.add_column("mail_fetch_records", sa.Column("classification_evidence", mysql.JSON()))
    op.add_column("mail_fetch_records", sa.Column("classified_at", mysql.DATETIME(fsp=3)))
    op.add_column("mail_fetch_records", sa.Column("completed_at", mysql.DATETIME(fsp=3)))
    op.create_foreign_key("fk_mail_fetch_records_thread", "mail_fetch_records", "email_threads", ["thread_id"], ["id"])
    op.create_index("idx_mail_fetch_records_stage", "mail_fetch_records", ["processing_stage", "created_at"])
    op.create_index("idx_mail_fetch_records_classification", "mail_fetch_records", ["handling_level", "intent_type"])
    op.create_index("idx_mail_fetch_records_thread", "mail_fetch_records", ["thread_id"])

    op.add_column("ai_call_logs", sa.Column("mail_fetch_record_id", mysql.BIGINT(unsigned=True)))
    op.create_foreign_key("fk_ai_logs_mail_fetch_record", "ai_call_logs", "mail_fetch_records", ["mail_fetch_record_id"], ["id"])
    op.create_index("idx_ai_logs_mail_fetch_record", "ai_call_logs", ["mail_fetch_record_id"])


def downgrade() -> None:
    op.drop_index("idx_ai_logs_mail_fetch_record", table_name="ai_call_logs")
    op.drop_constraint("fk_ai_logs_mail_fetch_record", "ai_call_logs", type_="foreignkey")
    op.drop_column("ai_call_logs", "mail_fetch_record_id")
    op.drop_index("idx_mail_fetch_records_thread", table_name="mail_fetch_records")
    op.drop_index("idx_mail_fetch_records_classification", table_name="mail_fetch_records")
    op.drop_index("idx_mail_fetch_records_stage", table_name="mail_fetch_records")
    op.drop_constraint("fk_mail_fetch_records_thread", "mail_fetch_records", type_="foreignkey")
    for column in (
        "completed_at", "classified_at", "classification_evidence", "classification_reason_code",
        "classification_confidence", "classification_version", "handling_level", "intent_type",
        "recovery_stage", "processing_stage", "thread_id", "references_header", "in_reply_to",
    ):
        op.drop_column("mail_fetch_records", column)
    op.drop_index("idx_emails_persistence_tier", table_name="emails")
    op.drop_column("emails", "classification_locked")
    op.drop_column("emails", "persistence_tier")
