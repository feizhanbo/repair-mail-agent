"""extend processing trace, logs and reusable job runs

Revision ID: c4d8e6f1a2b3
Revises: b3e1f7d2a4c0
Create Date: 2026-07-14 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "c4d8e6f1a2b3"
down_revision = "b3e1f7d2a4c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("emails", sa.Column("processing_trace_id", sa.String(length=100), nullable=True))
    op.add_column("emails", sa.Column("source_content_sha256", mysql.CHAR(length=64), nullable=True))
    op.execute("UPDATE emails SET processing_trace_id = CONCAT('legacy-', id) WHERE processing_trace_id IS NULL")
    op.execute(
        """
        UPDATE emails
        SET source_content_sha256 = JSON_UNQUOTE(JSON_EXTRACT(raw_headers, '$.raw_eml_sha256'))
        WHERE raw_headers IS NOT NULL
          AND JSON_UNQUOTE(JSON_EXTRACT(raw_headers, '$.raw_eml_sha256')) REGEXP '^[0-9a-fA-F]{64}$'
        """
    )
    op.execute(
        """
        UPDATE emails AS duplicate_email
        JOIN (
            SELECT source_content_sha256, MIN(id) AS canonical_id
            FROM emails
            WHERE source_content_sha256 IS NOT NULL
            GROUP BY source_content_sha256
            HAVING COUNT(*) > 1
        ) AS duplicates
          ON duplicates.source_content_sha256 = duplicate_email.source_content_sha256
        SET duplicate_email.duplicate_of_email_id = duplicates.canonical_id,
            duplicate_email.source_content_sha256 = NULL
        WHERE duplicate_email.id <> duplicates.canonical_id
        """
    )
    op.create_index("idx_emails_processing_trace", "emails", ["processing_trace_id"], unique=False)
    op.create_unique_constraint("uk_emails_source_content_sha256", "emails", ["source_content_sha256"])

    op.add_column("operation_logs", sa.Column("correlation_id", sa.String(length=100), nullable=True))
    op.add_column("operation_logs", sa.Column("email_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("operation_logs", sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.create_foreign_key("fk_operation_logs_email", "operation_logs", "emails", ["email_id"], ["id"])
    op.create_foreign_key("fk_operation_logs_ticket", "operation_logs", "repair_tickets", ["ticket_id"], ["id"])
    op.create_index("idx_operation_logs_correlation", "operation_logs", ["correlation_id"], unique=False)
    op.create_index("idx_operation_logs_email", "operation_logs", ["email_id", "created_at"], unique=False)
    op.create_index("idx_operation_logs_ticket", "operation_logs", ["ticket_id", "created_at"], unique=False)

    for name, column in (
        ("event_stage", sa.Column("event_stage", sa.String(length=50), nullable=True)),
        ("event_status", sa.Column("event_status", sa.String(length=30), nullable=True)),
        ("target_type", sa.Column("target_type", sa.String(length=50), nullable=True)),
        ("target_id", sa.Column("target_id", mysql.BIGINT(unsigned=True), nullable=True)),
        ("duration_ms", sa.Column("duration_ms", sa.Integer(), nullable=True)),
        ("error_code", sa.Column("error_code", sa.String(length=100), nullable=True)),
    ):
        del name
        op.add_column("system_event_logs", column)
    op.create_index("idx_system_logs_stage_status", "system_event_logs", ["event_stage", "event_status", "created_at"], unique=False)
    op.create_index("idx_system_logs_target", "system_event_logs", ["target_type", "target_id"], unique=False)

    op.add_column("ai_call_logs", sa.Column("job_run_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("ai_call_logs", sa.Column("attachment_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("ai_call_logs", sa.Column("correlation_id", sa.String(length=100), nullable=True))
    op.add_column("ai_call_logs", sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False))
    op.add_column("ai_call_logs", sa.Column("error_code", sa.String(length=100), nullable=True))
    op.add_column("ai_call_logs", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("ai_call_logs", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column("ai_call_logs", sa.Column("total_tokens", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_ai_logs_job", "ai_call_logs", "job_run_logs", ["job_run_id"], ["id"])
    op.create_foreign_key("fk_ai_logs_attachment", "ai_call_logs", "email_attachments", ["attachment_id"], ["id"])
    op.create_index("idx_ai_logs_job", "ai_call_logs", ["job_run_id"], unique=False)
    op.create_index("idx_ai_logs_attachment", "ai_call_logs", ["attachment_id"], unique=False)
    op.create_index("idx_ai_logs_correlation", "ai_call_logs", ["correlation_id"], unique=False)

    op.alter_column("job_run_logs", "started_at", existing_type=mysql.DATETIME(fsp=3), nullable=True)
    op.add_column("job_run_logs", sa.Column("resource_type", sa.String(length=50), nullable=True))
    op.add_column("job_run_logs", sa.Column("resource_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("job_run_logs", sa.Column("correlation_id", sa.String(length=100), nullable=True))
    op.add_column("job_run_logs", sa.Column("idempotency_key", sa.String(length=191), nullable=True))
    op.add_column("job_run_logs", sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("job_run_logs", sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False))
    op.add_column("job_run_logs", sa.Column("next_run_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("job_run_logs", sa.Column("locked_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("job_run_logs", sa.Column("locked_by", sa.String(length=100), nullable=True))
    op.add_column("job_run_logs", sa.Column("error_code", sa.String(length=100), nullable=True))
    op.add_column("job_run_logs", sa.Column("result_json", mysql.JSON(), nullable=True))
    op.add_column("job_run_logs", sa.Column("input_oss_object_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("job_run_logs", sa.Column("output_oss_object_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column(
        "job_run_logs",
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
    )
    op.create_foreign_key("fk_job_run_logs_input_oss", "job_run_logs", "oss_objects", ["input_oss_object_id"], ["id"])
    op.create_foreign_key("fk_job_run_logs_output_oss", "job_run_logs", "oss_objects", ["output_oss_object_id"], ["id"])
    op.create_index("idx_job_run_logs_queue", "job_run_logs", ["status", "next_run_at", "created_at"], unique=False)
    op.create_index("idx_job_run_logs_resource", "job_run_logs", ["resource_type", "resource_id"], unique=False)
    op.create_index("idx_job_run_logs_correlation", "job_run_logs", ["correlation_id"], unique=False)
    op.create_unique_constraint("uk_job_run_logs_idempotency", "job_run_logs", ["idempotency_key"])

    op.add_column("mail_fetch_records", sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False))
    op.add_column("mail_fetch_records", sa.Column("last_attempt_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("mail_fetch_records", sa.Column("next_retry_at", mysql.DATETIME(fsp=3), nullable=True))
    op.create_index("idx_mail_fetch_records_retry", "mail_fetch_records", ["fetch_status", "next_retry_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_mail_fetch_records_retry", table_name="mail_fetch_records")
    op.drop_column("mail_fetch_records", "next_retry_at")
    op.drop_column("mail_fetch_records", "last_attempt_at")
    op.drop_column("mail_fetch_records", "attempt_count")

    op.drop_constraint("uk_job_run_logs_idempotency", "job_run_logs", type_="unique")
    op.drop_index("idx_job_run_logs_correlation", table_name="job_run_logs")
    op.drop_index("idx_job_run_logs_resource", table_name="job_run_logs")
    op.drop_index("idx_job_run_logs_queue", table_name="job_run_logs")
    op.drop_constraint("fk_job_run_logs_output_oss", "job_run_logs", type_="foreignkey")
    op.drop_constraint("fk_job_run_logs_input_oss", "job_run_logs", type_="foreignkey")
    for column in (
        "updated_at", "output_oss_object_id", "input_oss_object_id", "result_json", "error_code",
        "locked_by", "locked_at", "next_run_at", "max_attempts", "attempt_count", "idempotency_key",
        "correlation_id", "resource_id", "resource_type",
    ):
        op.drop_column("job_run_logs", column)
    op.alter_column("job_run_logs", "started_at", existing_type=mysql.DATETIME(fsp=3), nullable=False)

    op.drop_index("idx_ai_logs_correlation", table_name="ai_call_logs")
    op.drop_index("idx_ai_logs_attachment", table_name="ai_call_logs")
    op.drop_index("idx_ai_logs_job", table_name="ai_call_logs")
    op.drop_constraint("fk_ai_logs_attachment", "ai_call_logs", type_="foreignkey")
    op.drop_constraint("fk_ai_logs_job", "ai_call_logs", type_="foreignkey")
    for column in (
        "total_tokens", "output_tokens", "input_tokens", "error_code", "attempt_count",
        "correlation_id", "attachment_id", "job_run_id",
    ):
        op.drop_column("ai_call_logs", column)

    op.drop_index("idx_system_logs_target", table_name="system_event_logs")
    op.drop_index("idx_system_logs_stage_status", table_name="system_event_logs")
    for column in ("error_code", "duration_ms", "target_id", "target_type", "event_status", "event_stage"):
        op.drop_column("system_event_logs", column)

    op.drop_index("idx_operation_logs_ticket", table_name="operation_logs")
    op.drop_index("idx_operation_logs_email", table_name="operation_logs")
    op.drop_index("idx_operation_logs_correlation", table_name="operation_logs")
    op.drop_constraint("fk_operation_logs_ticket", "operation_logs", type_="foreignkey")
    op.drop_constraint("fk_operation_logs_email", "operation_logs", type_="foreignkey")
    op.drop_column("operation_logs", "ticket_id")
    op.drop_column("operation_logs", "email_id")
    op.drop_column("operation_logs", "correlation_id")

    op.drop_constraint("uk_emails_source_content_sha256", "emails", type_="unique")
    op.drop_index("idx_emails_processing_trace", table_name="emails")
    op.drop_column("emails", "source_content_sha256")
    op.drop_column("emails", "processing_trace_id")
