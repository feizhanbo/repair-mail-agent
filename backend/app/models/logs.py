from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, created_at_column, datetime_column, pk_column, updated_at_column


class AiCallLog(CreatedAtMixin, Base):
    __tablename__ = "ai_call_logs"
    __table_args__ = (
        Index("idx_ai_logs_trace", "trace_id"),
        Index("idx_ai_logs_email", "email_id"),
        Index("idx_ai_logs_ticket", "ticket_id"),
        Index("idx_ai_logs_job", "job_run_id"),
        Index("idx_ai_logs_attachment", "attachment_id"),
        Index("idx_ai_logs_correlation", "correlation_id"),
        Index("idx_ai_logs_call_type", "call_type"),
        Index("idx_ai_logs_ticket_call_time", "ticket_id", "call_type", "created_at"),
        Index("idx_ai_logs_status", "status", "created_at"),
        CheckConstraint("confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)", name="confidence_between_0_and_1"),
    )

    id: Mapped[int] = pk_column()
    trace_id: Mapped[str] = mapped_column(String(100), nullable=False)
    email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_ai_logs_email"))
    ticket_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_ai_logs_ticket"))
    job_run_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("job_run_logs.id", name="fk_ai_logs_job"))
    attachment_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("email_attachments.id", name="fk_ai_logs_attachment"))
    correlation_id: Mapped[str | None] = mapped_column(String(100))
    call_type: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    input_summary: Mapped[str | None] = mapped_column(String(1000))
    output_summary: Mapped[str | None] = mapped_column(String(1000))
    parsed_key_result: Mapped[dict | None] = mapped_column(mysql.JSON)
    confidence_score: Mapped[Any | None] = mapped_column(mysql.DECIMAL(5, 4))
    latency_ms: Mapped[int | None] = mapped_column()
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="1")
    error_code: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    total_tokens: Mapped[int | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="success")
    error_message: Mapped[str | None] = mapped_column(Text)
    log_file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    log_line_no: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    log_record_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))

    email: Mapped["Email | None"] = relationship(
        "Email", foreign_keys=[email_id], back_populates="ai_call_logs", lazy="raise"
    )
    ticket: Mapped["RepairTicket | None"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="ai_call_logs", lazy="raise"
    )
    job_run: Mapped["JobRunLog | None"] = relationship(
        "JobRunLog", foreign_keys=[job_run_id], back_populates="ai_call_logs", lazy="raise"
    )
    attachment: Mapped["EmailAttachment | None"] = relationship(
        "EmailAttachment", foreign_keys=[attachment_id], back_populates="ai_call_logs", lazy="raise"
    )
    reply_records: Mapped[list["ReplyRecord"]] = relationship(
        "ReplyRecord", foreign_keys="ReplyRecord.ai_call_log_id", back_populates="ai_call_log", lazy="raise"
    )


class OperationLog(CreatedAtMixin, Base):
    __tablename__ = "operation_logs"
    __table_args__ = (
        Index("idx_operation_logs_user", "user_id", "created_at"),
        Index("idx_operation_logs_target", "target_type", "target_id"),
        Index("idx_operation_logs_type", "operation_type", "created_at"),
        Index("idx_operation_logs_correlation", "correlation_id"),
        Index("idx_operation_logs_email", "email_id", "created_at"),
        Index("idx_operation_logs_ticket", "ticket_id", "created_at"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_operation_logs_user"))
    correlation_id: Mapped[str | None] = mapped_column(String(100))
    email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_operation_logs_email"))
    ticket_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_operation_logs_ticket"))
    operation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    description: Mapped[str | None] = mapped_column(String(500))
    before_data: Mapped[dict | None] = mapped_column(mysql.JSON)
    after_data: Mapped[dict | None] = mapped_column(mysql.JSON)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(500))

    email: Mapped["Email | None"] = relationship(
        "Email", foreign_keys=[email_id], back_populates="operation_logs", lazy="raise"
    )
    ticket: Mapped["RepairTicket | None"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="operation_logs", lazy="raise"
    )


class SystemEventLog(CreatedAtMixin, Base):
    __tablename__ = "system_event_logs"
    __table_args__ = (
        Index("idx_system_logs_type_time", "event_type", "created_at"),
        Index("idx_system_logs_severity", "severity", "created_at"),
        Index("idx_system_logs_correlation", "correlation_id"),
        Index("idx_system_logs_email", "email_id"),
        Index("idx_system_logs_ticket", "ticket_id"),
        Index("idx_system_logs_job", "job_run_id"),
        Index("idx_system_logs_stage_status", "event_stage", "event_status", "created_at"),
        Index("idx_system_logs_target", "target_type", "target_id"),
    )

    id: Mapped[int] = pk_column()
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, server_default="info")
    module_name: Mapped[str] = mapped_column(String(100), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(100))
    email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_system_logs_email"))
    ticket_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_system_logs_ticket"))
    job_run_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("job_run_logs.id", name="fk_system_logs_job"))
    event_stage: Mapped[str | None] = mapped_column(String(50))
    event_status: Mapped[str | None] = mapped_column(String(30))
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    duration_ms: Mapped[int | None] = mapped_column()
    error_code: Mapped[str | None] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    details: Mapped[dict | None] = mapped_column(mysql.JSON)
    stack_trace: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)

    email: Mapped["Email | None"] = relationship(
        "Email", foreign_keys=[email_id], back_populates="system_event_logs", lazy="raise"
    )
    ticket: Mapped["RepairTicket | None"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="system_event_logs", lazy="raise"
    )
    job_run: Mapped["JobRunLog | None"] = relationship(
        "JobRunLog", foreign_keys=[job_run_id], back_populates="system_event_logs", lazy="raise"
    )


class JobRunLog(Base):
    __tablename__ = "job_run_logs"
    __table_args__ = (
        Index("idx_job_run_logs_name_time", "job_name", "started_at"),
        Index("idx_job_run_logs_type_time", "job_type", "started_at"),
        Index("idx_job_run_logs_status", "status", "started_at"),
        Index("idx_job_run_logs_queue", "status", "next_run_at", "created_at"),
        Index("idx_job_run_logs_resource", "resource_type", "resource_id"),
        Index("idx_job_run_logs_correlation", "correlation_id"),
        UniqueConstraint("idempotency_key", name="uk_job_run_logs_idempotency"),
    )

    id: Mapped[int] = pk_column()
    job_name: Mapped[str] = mapped_column(String(100), nullable=False)
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="running")
    resource_type: Mapped[str | None] = mapped_column(String(50))
    resource_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    correlation_id: Mapped[str | None] = mapped_column(String(100))
    idempotency_key: Mapped[str | None] = mapped_column(String(191))
    started_at: Mapped[datetime | None] = datetime_column()
    finished_at: Mapped[datetime | None] = datetime_column()
    duration_ms: Mapped[int | None] = mapped_column()
    processed_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    success_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    failed_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(nullable=False, server_default="3")
    next_run_at: Mapped[datetime | None] = datetime_column()
    locked_at: Mapped[datetime | None] = datetime_column()
    locked_by: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", mysql.JSON)
    result_json: Mapped[dict | None] = mapped_column(mysql.JSON)
    input_oss_object_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("oss_objects.id", name="fk_job_run_logs_input_oss"))
    output_oss_object_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("oss_objects.id", name="fk_job_run_logs_output_oss"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    fetched_emails: Mapped[list["Email"]] = relationship(
        "Email", foreign_keys="Email.fetch_job_run_id", back_populates="fetch_job", lazy="raise"
    )
    ai_call_logs: Mapped[list[AiCallLog]] = relationship(
        AiCallLog, foreign_keys="AiCallLog.job_run_id", back_populates="job_run", lazy="raise"
    )
    system_event_logs: Mapped[list[SystemEventLog]] = relationship(
        SystemEventLog, foreign_keys="SystemEventLog.job_run_id", back_populates="job_run", lazy="raise"
    )
    input_oss_object: Mapped["OssObject | None"] = relationship(
        "OssObject", foreign_keys=[input_oss_object_id], back_populates="input_jobs", lazy="raise"
    )
    output_oss_object: Mapped["OssObject | None"] = relationship(
        "OssObject", foreign_keys=[output_oss_object_id], back_populates="output_jobs", lazy="raise"
    )

