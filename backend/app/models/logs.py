from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, created_at_column, datetime_column, pk_column


class AiCallLog(CreatedAtMixin, Base):
    __tablename__ = "ai_call_logs"
    __table_args__ = (
        Index("idx_ai_logs_trace", "trace_id"),
        Index("idx_ai_logs_email", "email_id"),
        Index("idx_ai_logs_ticket", "ticket_id"),
        Index("idx_ai_logs_call_type", "call_type"),
        Index("idx_ai_logs_ticket_call_time", "ticket_id", "call_type", "created_at"),
        Index("idx_ai_logs_status", "status", "created_at"),
        CheckConstraint("confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)", name="confidence_between_0_and_1"),
    )

    id: Mapped[int] = pk_column()
    trace_id: Mapped[str] = mapped_column(String(100), nullable=False)
    email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_ai_logs_email"))
    ticket_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_ai_logs_ticket"))
    call_type: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    input_summary: Mapped[str | None] = mapped_column(String(1000))
    output_summary: Mapped[str | None] = mapped_column(String(1000))
    parsed_key_result: Mapped[dict | None] = mapped_column(mysql.JSON)
    confidence_score: Mapped[Any | None] = mapped_column(mysql.DECIMAL(5, 4))
    latency_ms: Mapped[int | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="success")
    error_message: Mapped[str | None] = mapped_column(Text)
    log_file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    log_line_no: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    log_record_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))


class OperationLog(CreatedAtMixin, Base):
    __tablename__ = "operation_logs"
    __table_args__ = (
        Index("idx_operation_logs_user", "user_id", "created_at"),
        Index("idx_operation_logs_target", "target_type", "target_id"),
        Index("idx_operation_logs_type", "operation_type", "created_at"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_operation_logs_user"))
    operation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    description: Mapped[str | None] = mapped_column(String(500))
    before_data: Mapped[dict | None] = mapped_column(mysql.JSON)
    after_data: Mapped[dict | None] = mapped_column(mysql.JSON)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(500))


class SystemEventLog(CreatedAtMixin, Base):
    __tablename__ = "system_event_logs"
    __table_args__ = (
        Index("idx_system_logs_type_time", "event_type", "created_at"),
        Index("idx_system_logs_severity", "severity", "created_at"),
        Index("idx_system_logs_correlation", "correlation_id"),
        Index("idx_system_logs_email", "email_id"),
        Index("idx_system_logs_ticket", "ticket_id"),
        Index("idx_system_logs_job", "job_run_id"),
    )

    id: Mapped[int] = pk_column()
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, server_default="info")
    module_name: Mapped[str] = mapped_column(String(100), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(100))
    email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_system_logs_email"))
    ticket_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_system_logs_ticket"))
    job_run_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("job_run_logs.id", name="fk_system_logs_job"))
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    details: Mapped[dict | None] = mapped_column(mysql.JSON)
    stack_trace: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)


class JobRunLog(Base):
    __tablename__ = "job_run_logs"
    __table_args__ = (
        Index("idx_job_run_logs_name_time", "job_name", "started_at"),
        Index("idx_job_run_logs_type_time", "job_type", "started_at"),
        Index("idx_job_run_logs_status", "status", "started_at"),
    )

    id: Mapped[int] = pk_column()
    job_name: Mapped[str] = mapped_column(String(100), nullable=False)
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="running")
    started_at: Mapped[datetime] = created_at_column()
    finished_at: Mapped[datetime | None] = datetime_column()
    duration_ms: Mapped[int | None] = mapped_column()
    processed_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    success_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    failed_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", mysql.JSON)
    created_at: Mapped[datetime] = created_at_column()

