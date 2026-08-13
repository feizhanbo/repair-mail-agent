from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, datetime_column, pk_column


class WorkflowExecution(TimestampMixin, Base):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        UniqueConstraint("execution_id", name="uk_workflow_executions_execution"),
        UniqueConstraint("graph_thread_id", name="uk_workflow_executions_graph_thread"),
        Index("idx_workflow_executions_email", "email_id", "created_at"),
        Index("idx_workflow_executions_ticket", "ticket_id", "created_at"),
        Index("idx_workflow_executions_status", "status", "updated_at"),
    )

    id: Mapped[int] = pk_column()
    execution_id: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_thread_id: Mapped[str] = mapped_column(String(191), nullable=False)
    workflow_name: Mapped[str] = mapped_column(String(100), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(50), nullable=False)
    state_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default="shadow")
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="queued")
    email_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("emails.id", name="fk_workflow_executions_email", ondelete="SET NULL"),
    )
    ticket_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_tickets.id", name="fk_workflow_executions_ticket", ondelete="SET NULL"),
    )
    trigger_job_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("job_run_logs.id", name="fk_workflow_executions_job", ondelete="SET NULL"),
    )
    current_node: Mapped[str | None] = mapped_column(String(100))
    last_route: Mapped[str | None] = mapped_column(String(100))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    checkpoint_id: Mapped[str | None] = mapped_column(String(100))
    checkpoint_step: Mapped[int | None] = mapped_column()
    input_fingerprint: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    result_summary: Mapped[dict | None] = mapped_column(mysql.JSON)
    started_at: Mapped[datetime | None] = datetime_column()
    completed_at: Mapped[datetime | None] = datetime_column()


class WorkflowInterrupt(TimestampMixin, Base):
    __tablename__ = "workflow_interrupts"
    __table_args__ = (
        UniqueConstraint("execution_id", "interrupt_id", name="uk_workflow_interrupts_execution_interrupt"),
        Index("idx_workflow_interrupts_task", "manual_task_id"),
        Index("idx_workflow_interrupts_status", "status", "updated_at"),
    )

    id: Mapped[int] = pk_column()
    execution_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_executions.execution_id", name="fk_workflow_interrupts_execution", ondelete="CASCADE"),
        nullable=False,
    )
    interrupt_id: Mapped[str] = mapped_column(String(100), nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(100))
    checkpoint_step: Mapped[int | None] = mapped_column()
    manual_task_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("manual_review_tasks.id", name="fk_workflow_interrupts_manual_task", ondelete="SET NULL"),
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    request_payload: Mapped[dict] = mapped_column(mysql.JSON, nullable=False)
    response_payload: Mapped[dict | None] = mapped_column(mysql.JSON)
    expected_ticket_version: Mapped[int | None] = mapped_column()
    resumed_by_user_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("users.id", name="fk_workflow_interrupts_resumed_by", ondelete="SET NULL"),
    )
    resumed_at: Mapped[datetime | None] = datetime_column()
    error_message: Mapped[str | None] = mapped_column(Text)
