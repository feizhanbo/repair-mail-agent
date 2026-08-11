from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, TimestampMixin, bool_column, pk_column


class WorkflowStatus(TimestampMixin, Base):
    __tablename__ = "workflow_statuses"
    __table_args__ = (UniqueConstraint("status_code", name="uk_workflow_statuses_code"),)

    id: Mapped[int] = pk_column()
    status_code: Mapped[str] = mapped_column(String(50), nullable=False)
    status_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status_category: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    is_terminal: Mapped[bool] = bool_column(False)
    sort_order: Mapped[int] = mapped_column(nullable=False, server_default="0")
    enabled: Mapped[bool] = bool_column(True)


class WorkflowTransition(TimestampMixin, Base):
    __tablename__ = "workflow_transitions"
    __table_args__ = (
        UniqueConstraint("from_status_code", "to_status_code", "trigger_event", name="uk_workflow_transitions_rule"),
        Index("idx_workflow_transitions_from", "from_status_code"),
        Index("idx_workflow_transitions_to", "to_status_code"),
    )

    id: Mapped[int] = pk_column()
    from_status_code: Mapped[str] = mapped_column(String(50), ForeignKey("workflow_statuses.status_code", name="fk_transition_from_status"), nullable=False)
    to_status_code: Mapped[str] = mapped_column(String(50), ForeignKey("workflow_statuses.status_code", name="fk_transition_to_status"), nullable=False)
    trigger_event: Mapped[str] = mapped_column(String(100), nullable=False)
    condition_desc: Mapped[str | None] = mapped_column(String(500))
    require_manual: Mapped[bool] = bool_column(False)
    enabled: Mapped[bool] = bool_column(True)


class TicketStatusLog(CreatedAtMixin, Base):
    __tablename__ = "ticket_status_logs"
    __table_args__ = (
        Index("idx_status_logs_ticket", "ticket_id"),
        Index("idx_status_logs_created_at", "created_at"),
        Index("idx_status_logs_transition", "from_status_code", "to_status_code"),
        Index("idx_status_logs_user", "operator_user_id", "created_at"),
    )

    id: Mapped[int] = pk_column()
    ticket_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_status_logs_ticket", ondelete="CASCADE"), nullable=False)
    from_status_code: Mapped[str | None] = mapped_column(String(50), ForeignKey("workflow_statuses.status_code", name="fk_status_logs_from"))
    to_status_code: Mapped[str] = mapped_column(String(50), ForeignKey("workflow_statuses.status_code", name="fk_status_logs_to"), nullable=False)
    trigger_event: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    operator_type: Mapped[str] = mapped_column(String(30), nullable=False, server_default="system")
    operator_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_status_logs_user"))
    metadata_json: Mapped[dict | None] = mapped_column("metadata", mysql.JSON)

    ticket: Mapped["RepairTicket"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="status_logs", lazy="raise"
    )


class FieldAuditLog(CreatedAtMixin, Base):
    __tablename__ = "field_audit_logs"
    __table_args__ = (
        Index("idx_field_audit_ticket", "ticket_id"),
        Index("idx_field_audit_item", "ticket_item_id"),
        Index("idx_field_audit_field", "field_name"),
        Index("idx_field_audit_user", "operator_user_id", "created_at"),
    )

    id: Mapped[int] = pk_column()
    ticket_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_field_audit_ticket", ondelete="CASCADE"), nullable=False)
    ticket_item_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_ticket_items.id", name="fk_field_audit_item", ondelete="CASCADE"))
    field_name: Mapped[str] = mapped_column(String(100), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    operator_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_field_audit_user"))
    parse_result_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("parse_results.id", name="fk_field_audit_parse_result"))

    ticket: Mapped["RepairTicket"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="field_audit_logs", lazy="raise"
    )
    ticket_item: Mapped["RepairTicketItem | None"] = relationship(
        "RepairTicketItem", foreign_keys=[ticket_item_id], back_populates="field_audit_logs", lazy="raise"
    )
    parse_result: Mapped["ParseResult | None"] = relationship(
        "ParseResult", foreign_keys=[parse_result_id], back_populates="field_audit_logs", lazy="raise"
    )

