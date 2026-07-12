from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, pk_column


class GraphRun(CreatedAtMixin, Base):
    __tablename__ = "graph_runs"
    __table_args__ = (
        Index("idx_graph_runs_email", "email_id"),
        Index("idx_graph_runs_ticket", "ticket_id"),
        Index("idx_graph_runs_status", "status", "created_at"),
        Index("idx_graph_runs_thread_id", "graph_thread_id"),
        Index("idx_graph_runs_current_node", "current_node"),
    )

    id: Mapped[int] = pk_column()
    graph_run_id: Mapped[str] = mapped_column(mysql.CHAR(36), nullable=False, unique=True)
    graph_thread_id: Mapped[str | None] = mapped_column(mysql.CHAR(36), index=True)
    graph_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_graph_runs_email"))
    ticket_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_graph_runs_ticket"))
    current_node: Mapped[str | None] = mapped_column(String(100))
    trigger_source: Mapped[str] = mapped_column(String(50), nullable=False, server_default="api")
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="running")
    interrupt_type: Mapped[str | None] = mapped_column(String(50))
    interrupt_payload: Mapped[dict | None] = mapped_column(mysql.JSON)
    manual_task_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("manual_review_tasks.id", name="fk_graph_runs_manual_task"))
    reply_record_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("reply_records.id", name="fk_graph_runs_reply"))
    resume_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=3))
    finished_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=3))
    interrupted_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=3))
    resumed_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=3))
    expired_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=3))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", mysql.JSON)


class GraphNodeLog(CreatedAtMixin, Base):
    __tablename__ = "graph_node_logs"
    __table_args__ = (
        Index("idx_graph_node_logs_run", "graph_run_id"),
        Index("idx_graph_node_logs_status", "status"),
        Index("idx_graph_node_logs_node_type", "node_type"),
    )

    id: Mapped[int] = pk_column()
    graph_run_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("graph_runs.id", name="fk_graph_node_logs_run", ondelete="CASCADE"), nullable=False)
    node_name: Mapped[str] = mapped_column(String(100), nullable=False)
    node_type: Mapped[str] = mapped_column(String(50), nullable=False, server_default="service")
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="running")
    retry_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    llm_call_log_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("ai_call_logs.id", name="fk_graph_node_logs_ai_log"))
    token_usage: Mapped[dict | None] = mapped_column(mysql.JSON)
    cost_estimate: Mapped[Any | None] = mapped_column("cost_estimate", mysql.DECIMAL(10, 6))
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=3))
    finished_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=3))
