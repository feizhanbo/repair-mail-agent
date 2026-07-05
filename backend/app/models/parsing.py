from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, bool_column, created_at_column, datetime_column, pk_column


class ParseResult(CreatedAtMixin, Base):
    __tablename__ = "parse_results"
    __table_args__ = (
        Index("idx_parse_results_email", "email_id"),
        Index("idx_parse_results_attachment", "source_attachment_id"),
        Index("idx_parse_results_ticket", "ticket_id"),
        Index("idx_parse_results_parser", "parser_type", "parser_version"),
        Index("idx_parse_results_apply_status", "apply_status"),
        Index("idx_parse_results_accepted", "accepted"),
        CheckConstraint("confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)", name="confidence_between_0_and_1"),
    )

    id: Mapped[int] = pk_column()
    email_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_parse_results_email"), nullable=False)
    source_attachment_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("email_attachments.id", name="fk_parse_results_attachment"))
    ticket_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_parse_results_ticket"))
    parser_type: Mapped[str] = mapped_column(String(30), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(50))
    intent_type: Mapped[str | None] = mapped_column(String(50))
    extracted_fields: Mapped[dict | None] = mapped_column(mysql.JSON)
    extracted_items: Mapped[dict | None] = mapped_column(mysql.JSON)
    missing_fields: Mapped[dict | None] = mapped_column(mysql.JSON)
    conflict_fields: Mapped[dict | None] = mapped_column(mysql.JSON)
    confidence_score: Mapped[Any | None] = mapped_column(mysql.DECIMAL(5, 4))
    field_confidences: Mapped[dict | None] = mapped_column(mysql.JSON)
    evidence: Mapped[dict | None] = mapped_column(mysql.JSON)
    apply_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    applied_by_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_parse_results_applied_by"))
    applied_at: Mapped[datetime | None] = datetime_column()
    accepted: Mapped[bool] = bool_column(False)
    accepted_by_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_parse_results_accepted_by"))
    accepted_at: Mapped[datetime | None] = datetime_column()
    error_message: Mapped[str | None] = mapped_column(Text)


class SnValidationResult(Base):
    __tablename__ = "sn_validation_results"
    __table_args__ = (
        Index("idx_sn_validation_ticket", "ticket_id"),
        Index("idx_sn_validation_item", "ticket_item_id"),
        Index("idx_sn_validation_sn", "sn"),
        Index("idx_sn_validation_status", "result_status"),
        Index("idx_sn_validation_asset", "matched_sn_asset_id"),
    )

    id: Mapped[int] = pk_column()
    ticket_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_sn_validation_ticket"), nullable=False)
    ticket_item_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_ticket_items.id", name="fk_sn_validation_item", ondelete="CASCADE"))
    sn: Mapped[str] = mapped_column(String(100), nullable=False)
    matched_sn_asset_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("sn_assets.id", name="fk_sn_validation_asset"))
    check_exists: Mapped[bool | None] = mapped_column(mysql.TINYINT(display_width=1))
    check_valid: Mapped[bool | None] = mapped_column(mysql.TINYINT(display_width=1))
    check_customer_match: Mapped[bool | None] = mapped_column(mysql.TINYINT(display_width=1))
    check_material_match: Mapped[bool | None] = mapped_column(mysql.TINYINT(display_width=1))
    need_ship_to_beijing: Mapped[bool | None] = mapped_column(mysql.TINYINT(display_width=1))
    result_status: Mapped[str] = mapped_column(String(30), nullable=False)
    result_message: Mapped[str | None] = mapped_column(Text)
    checked_by: Mapped[str] = mapped_column(String(30), nullable=False, server_default="system")
    checked_at: Mapped[datetime] = created_at_column()

