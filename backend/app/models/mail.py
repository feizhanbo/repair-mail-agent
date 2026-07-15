from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, TimestampMixin, bool_column, datetime_column, pk_column


class EmailThread(TimestampMixin, Base):
    __tablename__ = "email_threads"
    __table_args__ = (
        UniqueConstraint("thread_key", name="uk_email_threads_key"),
        Index("idx_email_threads_subject", "normalized_subject"),
        Index("idx_email_threads_ticket", "ticket_id"),
        Index("idx_email_threads_latest", "latest_email_id"),
    )

    id: Mapped[int] = pk_column()
    thread_key: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_subject: Mapped[str | None] = mapped_column(String(500))
    root_message_id: Mapped[str | None] = mapped_column(String(500))
    latest_email_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("emails.id", use_alter=True, name="fk_email_threads_latest_email"),
    )
    ticket_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_tickets.id", use_alter=True, name="fk_email_threads_ticket"),
    )
    email_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    merge_confidence: Mapped[Any | None] = mapped_column(mysql.DECIMAL(5, 4))
    merge_reason: Mapped[str | None] = mapped_column(String(500))
    manual_locked: Mapped[bool] = bool_column(False)


class Email(TimestampMixin, Base):
    __tablename__ = "emails"
    __table_args__ = (
        UniqueConstraint("message_id", name="uk_emails_message_id"),
        Index("idx_emails_thread", "thread_id"),
        Index("idx_emails_received_at", "received_at"),
        Index("idx_emails_fetch_job", "fetch_job_run_id"),
        Index("idx_emails_direction_status_time", "mail_direction", "parse_status", "received_at"),
        Index("idx_emails_intent", "intent_type"),
        Index("idx_emails_from_domain", "from_domain"),
        Index("idx_emails_processing_trace", "processing_trace_id"),
        UniqueConstraint("source_content_sha256", name="uk_emails_source_content_sha256"),
    )

    id: Mapped[int] = pk_column()
    fetch_job_run_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("job_run_logs.id", name="fk_emails_fetch_job"))
    thread_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("email_threads.id", name="fk_emails_thread"))
    raw_eml_oss_object_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("oss_objects.id", name="fk_emails_raw_eml_oss"))
    processing_trace_id: Mapped[str | None] = mapped_column(String(100))
    source_content_sha256: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    mail_direction: Mapped[str] = mapped_column(String(20), nullable=False, server_default="inbound")
    mailbox_account: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_name: Mapped[str | None] = mapped_column(String(255))
    imap_uid: Mapped[str | None] = mapped_column(String(100))
    message_id: Mapped[str] = mapped_column(String(500), nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(500))
    references_header: Mapped[str | None] = mapped_column(Text)
    raw_headers: Mapped[dict | None] = mapped_column(mysql.JSON)
    from_address: Mapped[str] = mapped_column(String(500), nullable=False)
    from_domain: Mapped[str | None] = mapped_column(String(255))
    to_addresses: Mapped[str | None] = mapped_column(Text)
    cc_addresses: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(500))
    normalized_subject: Mapped[str | None] = mapped_column(String(500))
    sent_at: Mapped[datetime | None] = datetime_column()
    received_at: Mapped[datetime | None] = datetime_column()
    text_body: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    html_body: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    clean_body: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    latest_reply_segment: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    parse_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    intent_type: Mapped[str | None] = mapped_column(String(50))
    duplicate_of_email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_emails_duplicate_of"))
    error_message: Mapped[str | None] = mapped_column(Text)


class EmailAttachment(CreatedAtMixin, Base):
    __tablename__ = "email_attachments"
    __table_args__ = (
        Index("idx_email_attachments_email", "email_id"),
        Index("idx_email_attachments_oss", "oss_object_id"),
        Index("idx_email_attachments_hash", "file_hash"),
        Index("idx_email_attachments_content_id", "content_id"),
        Index("idx_email_attachments_parse_status", "parse_status"),
    )

    id: Mapped[int] = pk_column()
    email_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_email_attachments_email"), nullable=False)
    oss_object_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("oss_objects.id", name="fk_email_attachments_oss"))
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    file_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    is_inline: Mapped[bool] = bool_column(False)
    content_id: Mapped[str | None] = mapped_column(String(255))
    parse_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    extracted_text: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    extracted_json: Mapped[dict | None] = mapped_column(mysql.JSON)
    parse_error: Mapped[str | None] = mapped_column(Text)


class EmailTicketLink(CreatedAtMixin, Base):
    __tablename__ = "email_ticket_links"
    __table_args__ = (
        UniqueConstraint("email_id", "ticket_id", "link_type", name="uk_email_ticket_links"),
        Index("idx_email_ticket_links_email", "email_id"),
        Index("idx_email_ticket_links_ticket", "ticket_id"),
        Index("idx_email_ticket_links_type", "link_type"),
    )

    id: Mapped[int] = pk_column()
    email_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_email_ticket_links_email"), nullable=False)
    ticket_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_email_ticket_links_ticket"), nullable=False)
    link_type: Mapped[str] = mapped_column(String(30), nullable=False)
    link_reason: Mapped[str | None] = mapped_column(String(500))
    linked_by_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_email_ticket_links_user"))

