from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, bool_column, datetime_column, pk_column


class RepairTicket(TimestampMixin, Base):
    __tablename__ = "repair_tickets"
    __table_args__ = (
        UniqueConstraint("ticket_no", name="uk_repair_tickets_no"),
        Index("idx_repair_tickets_status_updated", "current_status_code", "updated_at"),
        Index("idx_repair_tickets_customer_code", "customer_code"),
        Index("idx_repair_tickets_customer_name", "customer_name"),
        Index("idx_repair_tickets_thread", "thread_id"),
        Index("idx_repair_tickets_assignee_status", "assigned_user_id", "current_status_code"),
        Index("idx_repair_tickets_relay_status", "relay_export_status", "updated_at"),
        Index("idx_repair_tickets_rma_status", "rma_status", "updated_at"),
        Index("idx_repair_tickets_sn_validation_status", "sn_validation_status", "updated_at"),
        Index("idx_repair_tickets_category_status", "ticket_category", "current_status_code"),
        Index("idx_repair_tickets_policy_status", "policy_resolution_status", "updated_at"),
        CheckConstraint("followup_count >= 0", name="followup_count_non_negative"),
        CheckConstraint("max_followup_count >= followup_count", name="max_followup_count_gte_followup_count"),
        CheckConstraint("confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)", name="confidence_between_0_and_1"),
    )

    id: Mapped[int] = pk_column()
    ticket_no: Mapped[str] = mapped_column(String(50), nullable=False)
    current_status_code: Mapped[str] = mapped_column(String(50), nullable=False, server_default="new_email")
    ticket_category: Mapped[str] = mapped_column(String(30), nullable=False, server_default="standard_repair")
    origin_handling_level: Mapped[str | None] = mapped_column(String(30))
    origin_intent_type: Mapped[str | None] = mapped_column(String(50))
    source_email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", use_alter=True, name="fk_repair_tickets_source_email"))
    thread_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("email_threads.id", use_alter=True, name="fk_repair_tickets_thread"))
    customer_code: Mapped[str | None] = mapped_column(String(50))
    customer_name: Mapped[str | None] = mapped_column(String(255))
    customer_scope: Mapped[str | None] = mapped_column(String(20))
    customer_scope_source: Mapped[str | None] = mapped_column(String(30))
    charge_status: Mapped[str | None] = mapped_column(String(30))
    charge_status_source: Mapped[str | None] = mapped_column(String(30))
    service_policy_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey(
            "customer_service_policies.id",
            name="fk_repair_tickets_service_policy",
            ondelete="SET NULL",
        ),
    )
    policy_resolution_status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="pending"
    )
    policy_snapshot: Mapped[dict | None] = mapped_column(mysql.JSON)
    contact_person: Mapped[str | None] = mapped_column(String(100))
    contact_phone: Mapped[str | None] = mapped_column(String(100))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    request_date: Mapped[date | None] = mapped_column(mysql.DATE)
    mailing_address: Mapped[str | None] = mapped_column(String(500))
    problem_description: Mapped[str | None] = mapped_column(Text)
    accessories: Mapped[str | None] = mapped_column(String(500))
    missing_fields: Mapped[dict | None] = mapped_column(mysql.JSON)
    conflict_fields: Mapped[dict | None] = mapped_column(mysql.JSON)
    followup_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    max_followup_count: Mapped[int] = mapped_column(nullable=False, server_default="3")
    confidence_score: Mapped[Any | None] = mapped_column(mysql.DECIMAL(5, 4))
    assigned_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_repair_tickets_assigned_user"))
    language_code: Mapped[str] = mapped_column(String(20), nullable=False, server_default="unknown")
    rma_required: Mapped[bool] = bool_column(False)
    relay_export_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="not_required")
    rma_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="not_required")
    sn_validation_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    sn_validation_snapshot: Mapped[dict | None] = mapped_column(mysql.JSON)
    sn_validation_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    sn_validated_at: Mapped[Any | None] = datetime_column()
    safety_check_snapshot: Mapped[dict | None] = mapped_column(mysql.JSON)
    safety_check_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    safety_checked_at: Mapped[Any | None] = datetime_column()
    terminal_reason_code: Mapped[str | None] = mapped_column(String(100))
    terminal_reason: Mapped[str | None] = mapped_column(String(500))
    closed_at: Mapped[Any | None] = datetime_column()
    resolved_at: Mapped[Any | None] = datetime_column()
    resolution_code: Mapped[str | None] = mapped_column(String(100))
    resolution_summary: Mapped[str | None] = mapped_column(String(500))
    manual_locked: Mapped[bool] = bool_column(False)
    version: Mapped[int] = mapped_column(nullable=False, server_default="1")

    source_email: Mapped["Email | None"] = relationship(
        "Email", foreign_keys=[source_email_id], back_populates="source_tickets", lazy="raise"
    )
    thread: Mapped["EmailThread | None"] = relationship(
        "EmailThread", foreign_keys=[thread_id], back_populates="tickets_using_thread", lazy="raise"
    )
    owned_threads: Mapped[list["EmailThread"]] = relationship(
        "EmailThread", foreign_keys="EmailThread.ticket_id", back_populates="ticket", lazy="raise"
    )
    predecessor_threads: Mapped[list["EmailThread"]] = relationship(
        "EmailThread", foreign_keys="EmailThread.predecessor_ticket_id", back_populates="predecessor_ticket", lazy="raise"
    )
    items: Mapped[list["RepairTicketItem"]] = relationship(
        "RepairTicketItem", foreign_keys="RepairTicketItem.ticket_id", back_populates="ticket", passive_deletes=True, lazy="raise"
    )
    email_links: Mapped[list["EmailTicketLink"]] = relationship(
        "EmailTicketLink", foreign_keys="EmailTicketLink.ticket_id", back_populates="ticket", lazy="raise"
    )
    parse_results: Mapped[list["ParseResult"]] = relationship(
        "ParseResult", foreign_keys="ParseResult.ticket_id", back_populates="ticket", lazy="raise"
    )
    validation_results: Mapped[list["SnValidationResult"]] = relationship(
        "SnValidationResult", foreign_keys="SnValidationResult.ticket_id", back_populates="ticket", lazy="raise"
    )
    manual_review_tasks: Mapped[list["ManualReviewTask"]] = relationship(
        "ManualReviewTask", foreign_keys="ManualReviewTask.ticket_id", back_populates="ticket", passive_deletes=True, lazy="raise"
    )
    notifications: Mapped[list["NotificationEvent"]] = relationship(
        "NotificationEvent", foreign_keys="NotificationEvent.ticket_id", back_populates="ticket", passive_deletes=True, lazy="raise"
    )
    status_logs: Mapped[list["TicketStatusLog"]] = relationship(
        "TicketStatusLog", foreign_keys="TicketStatusLog.ticket_id", back_populates="ticket", passive_deletes=True, lazy="raise"
    )
    field_audit_logs: Mapped[list["FieldAuditLog"]] = relationship(
        "FieldAuditLog", foreign_keys="FieldAuditLog.ticket_id", back_populates="ticket", passive_deletes=True, lazy="raise"
    )
    replies: Mapped[list["ReplyRecord"]] = relationship(
        "ReplyRecord", foreign_keys="ReplyRecord.ticket_id", back_populates="ticket", passive_deletes=True, lazy="raise"
    )
    relay_exports: Mapped[list["TicketRelayExport"]] = relationship(
        "TicketRelayExport", foreign_keys="TicketRelayExport.ticket_id", back_populates="ticket", passive_deletes=True, lazy="raise"
    )
    sap_exports: Mapped[list["ExportSap"]] = relationship(
        "ExportSap", foreign_keys="ExportSap.ticket_id", back_populates="ticket", passive_deletes=True, lazy="raise"
    )
    rmas: Mapped[list["TicketRma"]] = relationship(
        "TicketRma", foreign_keys="TicketRma.ticket_id", back_populates="ticket", passive_deletes=True, lazy="raise"
    )
    external_operations: Mapped[list["ExternalOperationRecord"]] = relationship(
        "ExternalOperationRecord", foreign_keys="ExternalOperationRecord.ticket_id", back_populates="ticket", passive_deletes=True, lazy="raise"
    )
    ai_call_logs: Mapped[list["AiCallLog"]] = relationship(
        "AiCallLog", foreign_keys="AiCallLog.ticket_id", back_populates="ticket", lazy="raise"
    )
    operation_logs: Mapped[list["OperationLog"]] = relationship(
        "OperationLog", foreign_keys="OperationLog.ticket_id", back_populates="ticket", lazy="raise"
    )
    system_event_logs: Mapped[list["SystemEventLog"]] = relationship(
        "SystemEventLog", foreign_keys="SystemEventLog.ticket_id", back_populates="ticket", lazy="raise"
    )


class RepairTicketItem(TimestampMixin, Base):
    __tablename__ = "repair_ticket_items"
    __table_args__ = (
        UniqueConstraint("ticket_id", "line_no", name="uk_ticket_items_line"),
        Index("idx_ticket_items_sn", "sn"),
        Index("idx_ticket_items_material_code", "material_code"),
        Index("idx_ticket_items_board_code", "board_code"),
        Index("idx_ticket_items_validation", "validation_status"),
        Index("idx_ticket_items_return_route", "return_route_status", "return_location"),
    )

    id: Mapped[int] = pk_column()
    ticket_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_ticket_items_ticket", ondelete="CASCADE"), nullable=False)
    line_no: Mapped[int] = mapped_column(nullable=False)
    material_code: Mapped[str | None] = mapped_column(String(100))
    material_name: Mapped[str | None] = mapped_column(String(255))
    board_code: Mapped[str | None] = mapped_column(String(100))
    board_name: Mapped[str | None] = mapped_column(String(255))
    matched_board_card_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey(
            "board_cards.id",
            name="fk_ticket_items_board_card",
            ondelete="SET NULL",
        ),
    )
    return_location: Mapped[str | None] = mapped_column(String(20))
    return_address: Mapped[str | None] = mapped_column(String(500))
    return_contact: Mapped[str | None] = mapped_column(String(100))
    return_phone: Mapped[str | None] = mapped_column(String(100))
    return_postal_code: Mapped[str | None] = mapped_column(String(20))
    return_route_source: Mapped[str | None] = mapped_column(String(30))
    return_route_status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="pending"
    )
    return_route_message: Mapped[str | None] = mapped_column(Text)
    return_route_snapshot: Mapped[dict | None] = mapped_column(mysql.JSON)
    sn: Mapped[str | None] = mapped_column(String(100))
    sn_asset_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("sn_assets.id", name="fk_ticket_items_sn_asset"))
    quantity: Mapped[int] = mapped_column(nullable=False, server_default="1")
    failure_description: Mapped[str | None] = mapped_column(Text)
    failure_information: Mapped[str | None] = mapped_column(Text)
    data_info: Mapped[str | None] = mapped_column(Text)
    remarks: Mapped[str | None] = mapped_column(Text)
    accessories: Mapped[str | None] = mapped_column(String(500))
    validation_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    validation_message: Mapped[str | None] = mapped_column(Text)
    manual_locked: Mapped[bool] = bool_column(False)

    ticket: Mapped[RepairTicket] = relationship(
        RepairTicket, foreign_keys=[ticket_id], back_populates="items", lazy="raise"
    )
    validation_results: Mapped[list["SnValidationResult"]] = relationship(
        "SnValidationResult", foreign_keys="SnValidationResult.ticket_item_id", back_populates="ticket_item", passive_deletes=True, lazy="raise"
    )
    field_audit_logs: Mapped[list["FieldAuditLog"]] = relationship(
        "FieldAuditLog", foreign_keys="FieldAuditLog.ticket_item_id", back_populates="ticket_item", passive_deletes=True, lazy="raise"
    )
    sap_exports: Mapped[list["ExportSap"]] = relationship(
        "ExportSap", foreign_keys="ExportSap.ticket_item_id", back_populates="ticket_item", passive_deletes=True, lazy="raise"
    )
    rma_item: Mapped["TicketRmaItem | None"] = relationship(
        "TicketRmaItem", foreign_keys="TicketRmaItem.ticket_item_id", back_populates="ticket_item", passive_deletes=True, lazy="raise"
    )

