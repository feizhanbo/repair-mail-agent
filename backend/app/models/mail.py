from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, TimestampMixin, bool_column, datetime_column, pk_column


class EmailThread(TimestampMixin, Base):
    __tablename__ = "email_threads"
    __table_args__ = (
        UniqueConstraint("thread_key", name="uk_email_threads_key"),
        Index("idx_email_threads_subject", "normalized_subject"),
        Index("idx_email_threads_ticket", "ticket_id"),
        Index("idx_email_threads_latest", "latest_email_id"),
        Index("idx_email_threads_predecessor", "predecessor_thread_id", "predecessor_ticket_id"),
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
    predecessor_thread_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("email_threads.id", use_alter=True, name="fk_email_threads_predecessor_thread"),
    )
    predecessor_ticket_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_tickets.id", use_alter=True, name="fk_email_threads_predecessor_ticket"),
    )
    thread_version: Mapped[int] = mapped_column(nullable=False, server_default="1")
    email_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    merge_confidence: Mapped[Any | None] = mapped_column(mysql.DECIMAL(5, 4))
    merge_reason: Mapped[str | None] = mapped_column(String(500))
    manual_locked: Mapped[bool] = bool_column(False)

    emails: Mapped[list["Email"]] = relationship(
        "Email", foreign_keys="Email.thread_id", back_populates="thread", lazy="raise"
    )
    latest_email: Mapped["Email | None"] = relationship(
        "Email", foreign_keys=[latest_email_id], post_update=True, lazy="raise"
    )
    ticket: Mapped["RepairTicket | None"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="owned_threads", lazy="raise"
    )
    tickets_using_thread: Mapped[list["RepairTicket"]] = relationship(
        "RepairTicket", foreign_keys="RepairTicket.thread_id", back_populates="thread", lazy="raise"
    )
    predecessor_thread: Mapped["EmailThread | None"] = relationship(
        "EmailThread",
        foreign_keys=[predecessor_thread_id],
        remote_side="EmailThread.id",
        back_populates="successor_threads",
        lazy="raise",
    )
    successor_threads: Mapped[list["EmailThread"]] = relationship(
        "EmailThread", foreign_keys="EmailThread.predecessor_thread_id", back_populates="predecessor_thread", lazy="raise"
    )
    predecessor_ticket: Mapped["RepairTicket | None"] = relationship(
        "RepairTicket", foreign_keys=[predecessor_ticket_id], back_populates="predecessor_threads", lazy="raise"
    )
    manual_review_tasks: Mapped[list["ManualReviewTask"]] = relationship(
        "ManualReviewTask", foreign_keys="ManualReviewTask.thread_id", back_populates="thread", lazy="raise"
    )


class Email(TimestampMixin, Base):
    __tablename__ = "emails"
    __table_args__ = (
        UniqueConstraint("message_id", name="uk_emails_message_id"),
        Index("idx_emails_thread", "thread_id"),
        Index("idx_emails_received_at", "received_at"),
        Index("idx_emails_fetch_job", "fetch_job_run_id"),
        Index("idx_emails_direction_status_time", "mail_direction", "parse_status", "received_at"),
        Index("idx_emails_intent", "intent_type"),
        Index("idx_emails_intent_subtype", "intent_subtype"),
        Index("idx_emails_handling_intent", "handling_level", "intent_type"),
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
    processing_stage: Mapped[str] = mapped_column(String(50), nullable=False, server_default="fetched")
    intent_type: Mapped[str | None] = mapped_column(String(50))
    intent_subtype: Mapped[str | None] = mapped_column(String(50))
    handling_level: Mapped[str | None] = mapped_column(String(30))
    classification_version: Mapped[str | None] = mapped_column(String(50))
    classification_confidence: Mapped[Any | None] = mapped_column(mysql.DECIMAL(5, 4))
    classification_reason_code: Mapped[str | None] = mapped_column(String(100))
    duplicate_of_email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_emails_duplicate_of"))
    terminal_reason_code: Mapped[str | None] = mapped_column(String(100))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    retryable: Mapped[bool] = bool_column(True)
    recovery_stage: Mapped[str | None] = mapped_column(String(100))
    next_retry_at: Mapped[datetime | None] = datetime_column()
    error_message: Mapped[str | None] = mapped_column(Text)

    fetch_job: Mapped["JobRunLog | None"] = relationship(
        "JobRunLog", foreign_keys=[fetch_job_run_id], back_populates="fetched_emails", lazy="raise"
    )
    thread: Mapped[EmailThread | None] = relationship(
        EmailThread, foreign_keys=[thread_id], back_populates="emails", lazy="raise"
    )
    raw_eml_oss_object: Mapped["OssObject | None"] = relationship(
        "OssObject", foreign_keys=[raw_eml_oss_object_id], back_populates="raw_emails", lazy="raise"
    )
    duplicate_of: Mapped["Email | None"] = relationship(
        "Email", foreign_keys=[duplicate_of_email_id], remote_side="Email.id", back_populates="duplicates", lazy="raise"
    )
    duplicates: Mapped[list["Email"]] = relationship(
        "Email", foreign_keys="Email.duplicate_of_email_id", back_populates="duplicate_of", lazy="raise"
    )
    attachments: Mapped[list["EmailAttachment"]] = relationship(
        "EmailAttachment", foreign_keys="EmailAttachment.email_id", back_populates="email", lazy="raise"
    )
    ticket_links: Mapped[list["EmailTicketLink"]] = relationship(
        "EmailTicketLink", foreign_keys="EmailTicketLink.email_id", back_populates="email", lazy="raise"
    )
    parse_results: Mapped[list["ParseResult"]] = relationship(
        "ParseResult", foreign_keys="ParseResult.email_id", back_populates="email", lazy="raise"
    )
    ai_call_logs: Mapped[list["AiCallLog"]] = relationship(
        "AiCallLog", foreign_keys="AiCallLog.email_id", back_populates="email", lazy="raise"
    )
    source_tickets: Mapped[list["RepairTicket"]] = relationship(
        "RepairTicket", foreign_keys="RepairTicket.source_email_id", back_populates="source_email", lazy="raise"
    )
    manual_review_tasks: Mapped[list["ManualReviewTask"]] = relationship(
        "ManualReviewTask", foreign_keys="ManualReviewTask.email_id", back_populates="email", lazy="raise"
    )
    related_reply_records: Mapped[list["ReplyRecord"]] = relationship(
        "ReplyRecord", foreign_keys="ReplyRecord.related_email_id", back_populates="related_email", lazy="raise"
    )
    outgoing_reply_records: Mapped[list["ReplyRecord"]] = relationship(
        "ReplyRecord", foreign_keys="ReplyRecord.outgoing_email_id", back_populates="outgoing_email", lazy="raise"
    )
    external_operations: Mapped[list["ExternalOperationRecord"]] = relationship(
        "ExternalOperationRecord", foreign_keys="ExternalOperationRecord.email_id", back_populates="email", lazy="raise"
    )
    operation_logs: Mapped[list["OperationLog"]] = relationship(
        "OperationLog", foreign_keys="OperationLog.email_id", back_populates="email", lazy="raise"
    )
    system_event_logs: Mapped[list["SystemEventLog"]] = relationship(
        "SystemEventLog", foreign_keys="SystemEventLog.email_id", back_populates="email", lazy="raise"
    )


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

    email: Mapped[Email] = relationship(
        Email, foreign_keys=[email_id], back_populates="attachments", lazy="raise"
    )
    oss_object: Mapped["OssObject | None"] = relationship(
        "OssObject", foreign_keys=[oss_object_id], back_populates="attachments", lazy="raise"
    )
    parse_results: Mapped[list["ParseResult"]] = relationship(
        "ParseResult", foreign_keys="ParseResult.source_attachment_id", back_populates="source_attachment", lazy="raise"
    )
    ai_call_logs: Mapped[list["AiCallLog"]] = relationship(
        "AiCallLog", foreign_keys="AiCallLog.attachment_id", back_populates="attachment", lazy="raise"
    )


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

    email: Mapped[Email] = relationship(
        Email, foreign_keys=[email_id], back_populates="ticket_links", lazy="raise"
    )
    ticket: Mapped["RepairTicket"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="email_links", lazy="raise"
    )

