"""add RMA issue tracking, thread lineage and external operation ledger

Revision ID: h5c9d0e1f2a3
Revises: g4b8c9d0e1f2
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "h5c9d0e1f2a3"
down_revision: str | None = "g4b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("emails", sa.Column("recovery_stage", sa.String(length=100), nullable=True))
    op.add_column(
        "manual_review_tasks",
        sa.Column("recovery_stage", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "manual_review_tasks",
        sa.Column("recovery_action", sa.String(length=500), nullable=True),
    )
    op.execute(
        """
        UPDATE manual_review_tasks
        SET recovery_stage = task_type,
            recovery_action = COALESCE(trigger_reason, '请核对异常原因并从对应业务阶段恢复。')
        WHERE status IN ('pending', 'assigned', 'claimed', 'assignment_failed')
        """
    )
    op.add_column(
        "email_threads",
        sa.Column("predecessor_thread_id", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.add_column(
        "email_threads",
        sa.Column("predecessor_ticket_id", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.add_column(
        "email_threads",
        sa.Column("thread_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_foreign_key(
        "fk_email_threads_predecessor_thread",
        "email_threads",
        "email_threads",
        ["predecessor_thread_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_email_threads_predecessor_ticket",
        "email_threads",
        "repair_tickets",
        ["predecessor_ticket_id"],
        ["id"],
    )
    op.create_index(
        "idx_email_threads_predecessor",
        "email_threads",
        ["predecessor_thread_id", "predecessor_ticket_id"],
        unique=False,
    )

    op.add_column(
        "reply_records",
        sa.Column("archive_status", sa.String(length=30), server_default="not_required", nullable=False),
    )
    op.add_column(
        "reply_records",
        sa.Column("send_attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "reply_records",
        sa.Column("archive_attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("reply_records", sa.Column("smtp_response", sa.String(length=500), nullable=True))
    op.add_column("reply_records", sa.Column("thread_version", sa.Integer(), nullable=True))
    op.add_column("reply_records", sa.Column("archive_verified_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("reply_records", sa.Column("next_retry_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("reply_records", sa.Column("last_error_code", sa.String(length=100), nullable=True))
    op.create_index(
        "idx_reply_records_archive_retry",
        "reply_records",
        ["archive_status", "next_retry_at"],
        unique=False,
    )
    op.execute(
        """
        UPDATE reply_records
        SET archive_status = CASE
                WHEN send_status = 'sent'
                     AND outgoing_email_id IS NOT NULL
                     AND (reply_type <> 'rma_authorization' OR rma_pdf_oss_object_id IS NOT NULL)
                    THEN 'legacy_unverified'
                WHEN send_status IN ('sent', 'sending', 'auto_sending', 'send_uncertain')
                    THEN 'pending'
                ELSE 'not_required'
            END,
            send_attempt_count = CASE WHEN send_status IN
                ('sending', 'auto_sending', 'sent', 'send_failed', 'send_uncertain') THEN 1 ELSE 0 END
        """
    )

    op.add_column("ticket_rmas", sa.Column("pdf_sha256", mysql.CHAR(length=64), nullable=True))
    op.add_column(
        "ticket_rmas",
        sa.Column("pdf_validation_status", sa.String(length=30), server_default="pending", nullable=False),
    )
    op.add_column(
        "ticket_rmas",
        sa.Column("pdf_archive_status", sa.String(length=30), server_default="pending", nullable=False),
    )
    op.add_column("ticket_rmas", sa.Column("pdf_archived_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("ticket_rmas", sa.Column("issued_at", mysql.DATETIME(fsp=3), nullable=True))
    op.execute(
        """
        UPDATE ticket_rmas
        SET pdf_validation_status = CASE
                WHEN pdf_oss_object_id IS NOT NULL THEN 'legacy_unverified'
                ELSE 'pending'
            END,
            pdf_archive_status = CASE
                WHEN pdf_oss_object_id IS NOT NULL THEN 'legacy_unverified'
                ELSE 'pending'
            END
        """
    )

    op.create_table(
        "external_operation_records",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("operation_type", sa.String(length=50), nullable=False),
        sa.Column("operation_key", sa.String(length=191), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="planned", nullable=False),
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("email_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("reply_record_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("export_sap_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("remote_reference", sa.String(length=500), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "retryable",
            mysql.TINYINT(display_width=1),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("recovery_stage", sa.String(length=100), nullable=True),
        sa.Column("next_retry_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("started_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("completed_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("details_json", mysql.JSON(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["repair_tickets.id"],
            name="fk_external_operations_ticket",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["email_id"],
            ["emails.id"],
            name="fk_external_operations_email",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reply_record_id"],
            ["reply_records.id"],
            name="fk_external_operations_reply",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["export_sap_id"],
            ["export_sap.id"],
            name="fk_external_operations_export_sap",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_operation_records")),
        sa.UniqueConstraint(
            "operation_type",
            "operation_key",
            name="uk_external_operation_type_key",
        ),
    )
    op.create_index(
        "idx_external_operation_status_retry",
        "external_operation_records",
        ["status", "next_retry_at"],
        unique=False,
    )
    op.create_index(
        "idx_external_operation_ticket",
        "external_operation_records",
        ["ticket_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_external_operation_email",
        "external_operation_records",
        ["email_id", "created_at"],
        unique=False,
    )

    op.execute(
        """
        INSERT INTO workflow_transitions
            (from_status_code, to_status_code, trigger_event, condition_desc, require_manual, enabled)
        SELECT 'rma_sent', 'closed', 'rma_issued_and_archived',
               '正式RMA已回填、PDF关键字段校验通过、邮件发送成功且PDF和出站邮件归档完成。', 0, 1
        WHERE EXISTS (SELECT 1 FROM workflow_statuses WHERE status_code='rma_sent')
          AND EXISTS (SELECT 1 FROM workflow_statuses WHERE status_code='closed')
        ON DUPLICATE KEY UPDATE
            condition_desc=VALUES(condition_desc),
            require_manual=0,
            enabled=1
        """
    )
    op.execute(
        "UPDATE workflow_transitions SET enabled=0 "
        "WHERE from_status_code='rma_sent' AND to_status_code='closed' "
        "AND trigger_event='device_receipt_ack_sent'"
    )
    op.execute(
        """
        UPDATE workflow_statuses
        SET description='SMTP已明确发送RMA成功，等待完成PDF与出站邮件归档核验。'
        WHERE status_code='rma_sent'
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM workflow_transitions "
        "WHERE from_status_code='rma_sent' AND to_status_code='closed' "
        "AND trigger_event='rma_issued_and_archived'"
    )
    op.execute(
        "UPDATE workflow_transitions SET enabled=1 "
        "WHERE from_status_code='rma_sent' AND to_status_code='closed' "
        "AND trigger_event='device_receipt_ack_sent'"
    )
    op.execute(
        """
        UPDATE workflow_statuses
        SET description='全部SN已取得同一RMA编号，RMA模板回复已在原邮件链发送成功。'
        WHERE status_code='rma_sent'
        """
    )

    op.drop_index("idx_external_operation_email", table_name="external_operation_records")
    op.drop_index("idx_external_operation_ticket", table_name="external_operation_records")
    op.drop_index("idx_external_operation_status_retry", table_name="external_operation_records")
    op.drop_table("external_operation_records")

    op.drop_column("ticket_rmas", "issued_at")
    op.drop_column("ticket_rmas", "pdf_archived_at")
    op.drop_column("ticket_rmas", "pdf_archive_status")
    op.drop_column("ticket_rmas", "pdf_validation_status")
    op.drop_column("ticket_rmas", "pdf_sha256")

    op.drop_index("idx_reply_records_archive_retry", table_name="reply_records")
    op.drop_column("reply_records", "last_error_code")
    op.drop_column("reply_records", "next_retry_at")
    op.drop_column("reply_records", "archive_verified_at")
    op.drop_column("reply_records", "thread_version")
    op.drop_column("reply_records", "smtp_response")
    op.drop_column("reply_records", "archive_attempt_count")
    op.drop_column("reply_records", "send_attempt_count")
    op.drop_column("reply_records", "archive_status")

    op.drop_index("idx_email_threads_predecessor", table_name="email_threads")
    op.drop_constraint(
        "fk_email_threads_predecessor_ticket",
        "email_threads",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_email_threads_predecessor_thread",
        "email_threads",
        type_="foreignkey",
    )
    op.drop_column("email_threads", "thread_version")
    op.drop_column("email_threads", "predecessor_ticket_id")
    op.drop_column("email_threads", "predecessor_thread_id")
    op.drop_column("manual_review_tasks", "recovery_action")
    op.drop_column("manual_review_tasks", "recovery_stage")
    op.drop_column("emails", "recovery_stage")
