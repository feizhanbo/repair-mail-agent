"""persist system config, remove intent subtype, and close evidence-complete RMA tickets

Revision ID: u8p3k4l5m6n7
Revises: t7o2j3k4l5m6
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "u8p3k4l5m6n7"
down_revision = "t7o2j3k4l5m6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_configs",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("config_key", sa.String(100), nullable=False),
        sa.Column("config_group", sa.String(50), nullable=False),
        sa.Column("value_type", sa.String(20), nullable=False),
        sa.Column("config_value", mysql.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(3)")),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(3)")),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], name="fk_system_configs_updated_by"),
        sa.UniqueConstraint("config_key", name="uk_system_configs_key"),
    )
    op.create_index("idx_system_configs_group", "system_configs", ["config_group"])

    op.drop_index("idx_emails_intent_subtype", table_name="emails")
    op.drop_column("emails", "intent_subtype")
    op.drop_index("idx_parse_results_intent_subtype", table_name="parse_results")
    op.drop_column("parse_results", "intent_subtype")

    op.execute(
        "UPDATE workflow_transitions SET enabled = 1, "
        "condition_desc = 'RMA回复发送成功且PDF与出站EML归档核验完成。' "
        "WHERE from_status_code = 'rma_sent' AND to_status_code = 'closed' "
        "AND trigger_event = 'rma_issued_and_archived'"
    )
    evidence = """
        EXISTS (
          SELECT 1 FROM ticket_rmas tr
          WHERE tr.ticket_id = repair_tickets.id
            AND tr.status = 'issued' AND tr.issued_at IS NOT NULL
            AND tr.pdf_archive_status = 'archived' AND tr.pdf_oss_object_id IS NOT NULL
        )
        AND EXISTS (
          SELECT 1 FROM reply_records rr
          JOIN emails oe ON oe.id = rr.outgoing_email_id
          JOIN email_attachments ea
            ON ea.email_id = oe.id AND ea.oss_object_id = rr.rma_pdf_oss_object_id
          WHERE rr.ticket_id = repair_tickets.id
            AND rr.reply_type = 'rma_authorization'
            AND rr.send_status = 'sent' AND rr.smtp_message_id IS NOT NULL
            AND rr.archive_status = 'archived' AND rr.archive_verified_at IS NOT NULL
            AND oe.raw_eml_oss_object_id IS NOT NULL
            AND rr.rma_pdf_oss_object_id IS NOT NULL
        )
    """
    op.execute(
        "INSERT INTO ticket_status_logs "
        "(ticket_id, from_status_code, to_status_code, trigger_event, reason, operator_type, metadata, created_at) "
        "SELECT id, 'rma_sent', 'closed', 'rma_issued_and_archived', "
        "'历史RMA发送与归档证据完整，迁移自动闭合。', 'migration', "
        "JSON_OBJECT('migration', 'u8p3k4l5m6n7', 'evidence_verified', TRUE), CURRENT_TIMESTAMP(3) "
        f"FROM repair_tickets WHERE current_status_code = 'rma_sent' AND {evidence}"
    )
    op.execute(
        "UPDATE repair_tickets SET current_status_code = 'closed', "
        "terminal_reason_code = 'rma_issued_and_archived', "
        "terminal_reason = 'RMA回复发送成功且PDF与出站EML归档核验完成。', "
        "closed_at = COALESCE(closed_at, CURRENT_TIMESTAMP(3)), version = version + 1 "
        f"WHERE current_status_code = 'rma_sent' AND {evidence}"
    )


def downgrade() -> None:
    op.add_column("parse_results", sa.Column("intent_subtype", sa.String(50), nullable=True))
    op.create_index("idx_parse_results_intent_subtype", "parse_results", ["intent_subtype"])
    op.add_column("emails", sa.Column("intent_subtype", sa.String(50), nullable=True))
    op.create_index("idx_emails_intent_subtype", "emails", ["intent_subtype"])
    op.execute(
        "UPDATE workflow_transitions SET enabled = 0 "
        "WHERE from_status_code = 'rma_sent' AND to_status_code = 'closed' "
        "AND trigger_event = 'rma_issued_and_archived'"
    )
    op.drop_index("idx_system_configs_group", table_name="system_configs")
    op.drop_table("system_configs")
