from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

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
        Index("idx_repair_tickets_device_ack_status", "device_receipt_ack_status", "updated_at"),
        Index("idx_repair_tickets_policy_status", "policy_resolution_status", "updated_at"),
        CheckConstraint("followup_count >= 0", name="followup_count_non_negative"),
        CheckConstraint("max_followup_count >= followup_count", name="max_followup_count_gte_followup_count"),
        CheckConstraint("confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)", name="confidence_between_0_and_1"),
    )

    id: Mapped[int] = pk_column()
    ticket_no: Mapped[str] = mapped_column(String(50), nullable=False)
    current_status_code: Mapped[str] = mapped_column(String(50), nullable=False, server_default="new_email")
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
    device_received_at: Mapped[Any | None] = datetime_column()
    device_received_source: Mapped[str | None] = mapped_column(String(30))
    device_received_email_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("emails.id", use_alter=True, name="fk_repair_tickets_device_received_email"),
    )
    device_received_note: Mapped[str | None] = mapped_column(Text)
    device_received_idempotency_key: Mapped[str | None] = mapped_column(String(100))
    device_receipt_ack_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="not_received")
    terminal_reason_code: Mapped[str | None] = mapped_column(String(100))
    terminal_reason: Mapped[str | None] = mapped_column(String(500))
    closed_at: Mapped[Any | None] = datetime_column()
    manual_locked: Mapped[bool] = bool_column(False)
    version: Mapped[int] = mapped_column(nullable=False, server_default="1")


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

