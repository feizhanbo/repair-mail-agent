from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, bool_column, datetime_column, pk_column


class ReplyTemplate(TimestampMixin, Base):
    __tablename__ = "reply_templates"
    __table_args__ = (
        UniqueConstraint("template_code", "version", name="uk_reply_templates_code_version"),
        Index("idx_reply_templates_type_enabled", "template_type", "enabled"),
    )

    id: Mapped[int] = pk_column()
    template_code: Mapped[str] = mapped_column(String(100), nullable=False)
    template_name: Mapped[str] = mapped_column(String(100), nullable=False)
    template_type: Mapped[str] = mapped_column(String(50), nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False, server_default="zh-CN")
    version: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_template: Mapped[str | None] = mapped_column(String(500))
    body_template: Mapped[str] = mapped_column(mysql.MEDIUMTEXT, nullable=False)
    html_body_template: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    enabled: Mapped[bool] = bool_column(True)
    created_by_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_reply_templates_user"))


class ReplyRecord(TimestampMixin, Base):
    __tablename__ = "reply_records"
    __table_args__ = (
        Index("idx_reply_records_ticket", "ticket_id"),
        Index("idx_reply_records_status", "send_status"),
        Index("idx_reply_records_related_email", "related_email_id"),
        Index("idx_reply_records_outgoing_email", "outgoing_email_id"),
        Index("idx_reply_records_rma_pdf", "rma_pdf_oss_object_id"),
        Index("idx_reply_records_ticket_round", "ticket_id", "followup_round"),
        Index("idx_reply_records_review_status", "review_status", "created_at"),
        Index("idx_reply_records_archive_retry", "archive_status", "next_retry_at"),
    )

    id: Mapped[int] = pk_column()
    ticket_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_reply_records_ticket", ondelete="CASCADE"), nullable=False)
    related_email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_reply_records_related_email"))
    outgoing_email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_reply_records_outgoing_email"))
    template_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("reply_templates.id", name="fk_reply_records_template"))
    base_template_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("reply_templates.id", name="fk_reply_records_base_template"),
    )
    reply_type: Mapped[str] = mapped_column(String(50), nullable=False)
    followup_round: Mapped[int] = mapped_column(nullable=False, server_default="1")
    missing_fields: Mapped[dict | None] = mapped_column(mysql.JSON)
    to_addresses: Mapped[str] = mapped_column(Text, nullable=False)
    cc_addresses: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(500))
    draft_body: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    final_body: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    draft_html_body: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    final_html_body: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    thread_history_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    render_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    generate_source: Mapped[str] = mapped_column(String(30), nullable=False, server_default="system")
    ai_call_log_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("ai_call_logs.id", name="fk_reply_records_ai_log"))
    rma_pdf_oss_object_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("oss_objects.id", name="fk_reply_records_rma_pdf"))
    reply_template_version: Mapped[str | None] = mapped_column(String(100))
    rma_template_version: Mapped[str | None] = mapped_column(String(100))
    rma_pdf_data_snapshot: Mapped[dict | None] = mapped_column(mysql.JSON)
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    reviewed_by_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_reply_records_reviewer"))
    reviewed_at: Mapped[datetime | None] = datetime_column()
    send_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="draft")
    archive_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="not_required")
    send_attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    archive_attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    smtp_message_id: Mapped[str | None] = mapped_column(String(500))
    smtp_response: Mapped[str | None] = mapped_column(String(500))
    thread_version: Mapped[int | None] = mapped_column()
    in_reply_to: Mapped[str | None] = mapped_column(String(500))
    references_header: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = datetime_column()
    archive_verified_at: Mapped[datetime | None] = datetime_column()
    next_retry_at: Mapped[datetime | None] = datetime_column()
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)

    ticket: Mapped["RepairTicket"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="replies", lazy="raise"
    )
    related_email: Mapped["Email | None"] = relationship(
        "Email", foreign_keys=[related_email_id], back_populates="related_reply_records", lazy="raise"
    )
    outgoing_email: Mapped["Email | None"] = relationship(
        "Email", foreign_keys=[outgoing_email_id], back_populates="outgoing_reply_records", lazy="raise"
    )
    ai_call_log: Mapped["AiCallLog | None"] = relationship(
        "AiCallLog", foreign_keys=[ai_call_log_id], back_populates="reply_records", lazy="raise"
    )
    rma_pdf_oss_object: Mapped["OssObject | None"] = relationship(
        "OssObject", foreign_keys=[rma_pdf_oss_object_id], back_populates="reply_pdf_records", lazy="raise"
    )
    ticket_rmas: Mapped[list["TicketRma"]] = relationship(
        "TicketRma", foreign_keys="TicketRma.reply_record_id", back_populates="reply_record", lazy="raise"
    )
    external_operations: Mapped[list["ExternalOperationRecord"]] = relationship(
        "ExternalOperationRecord", foreign_keys="ExternalOperationRecord.reply_record_id", back_populates="reply_record", passive_deletes=True, lazy="raise"
    )

