from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, datetime_column, pk_column


class MailboxSyncState(TimestampMixin, Base):
    __tablename__ = "mailbox_sync_states"
    __table_args__ = (
        UniqueConstraint("mailbox_account", "folder_name", name="uk_mailbox_sync_account_folder"),
        Index("idx_mailbox_sync_mode_lease", "sync_mode", "lease_expires_at"),
    )

    id: Mapped[int] = pk_column()
    mailbox_account: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    uid_validity: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    sync_mode: Mapped[str] = mapped_column(String(30), nullable=False, server_default="paused")
    initial_sync_start_at: Mapped[datetime | None] = datetime_column()
    initial_sync_completed_at: Mapped[datetime | None] = datetime_column()
    last_discovered_uid: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    last_fetched_uid: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    last_sync_at: Mapped[datetime | None] = datetime_column()
    last_success_at: Mapped[datetime | None] = datetime_column()
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = datetime_column()
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(nullable=False, server_default="1")


class EmailOutbox(TimestampMixin, Base):
    __tablename__ = "email_outbox"
    __table_args__ = (
        UniqueConstraint("reply_record_id", name="uk_email_outbox_reply"),
        UniqueConstraint("idempotency_key", name="uk_email_outbox_idempotency"),
        UniqueConstraint("message_id", name="uk_email_outbox_message_id"),
        Index("idx_email_outbox_claim", "status", "next_attempt_at", "lease_expires_at"),
        Index("idx_email_outbox_ticket", "ticket_id", "created_at"),
    )

    id: Mapped[int] = pk_column()
    reply_record_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("reply_records.id", name="fk_email_outbox_reply", ondelete="CASCADE"),
        nullable=False,
    )
    ticket_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_tickets.id", name="fk_email_outbox_ticket", ondelete="CASCADE"),
        nullable=False,
    )
    related_email_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("emails.id", name="fk_email_outbox_related_email", ondelete="SET NULL"),
    )
    frozen_eml_oss_object_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("oss_objects.id", name="fk_email_outbox_frozen_eml_oss"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    message_id: Mapped[str] = mapped_column(String(500), nullable=False)
    from_address: Mapped[str] = mapped_column(String(500), nullable=False)
    to_addresses: Mapped[str] = mapped_column(Text, nullable=False)
    cc_addresses: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    frozen_eml_sha256: Mapped[str] = mapped_column(mysql.CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="preparing")
    ticket_version: Mapped[int | None] = mapped_column()
    thread_history_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    request_id: Mapped[str | None] = mapped_column(mysql.CHAR(36))
    rma_no: Mapped[str | None] = mapped_column(String(100))
    template_version: Mapped[str | None] = mapped_column(String(100))
    pdf_sha256: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    safety_snapshot: Mapped[dict | None] = mapped_column(mysql.JSON)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = datetime_column()
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime | None] = datetime_column()
    smtp_response: Mapped[str | None] = mapped_column(String(1000))
    accepted_at: Mapped[datetime | None] = datetime_column()
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(Text)

    reply_record: Mapped["ReplyRecord"] = relationship("ReplyRecord", foreign_keys=[reply_record_id], lazy="raise")
    frozen_eml_oss_object: Mapped["OssObject"] = relationship(
        "OssObject", foreign_keys=[frozen_eml_oss_object_id], lazy="raise"
    )


class MailDeliveryEvent(TimestampMixin, Base):
    __tablename__ = "mail_delivery_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uk_mail_delivery_event_key"),
        Index("idx_mail_delivery_outbox", "outbox_id", "created_at"),
        Index("idx_mail_delivery_ticket", "ticket_id", "created_at"),
        Index("idx_mail_delivery_status", "delivery_status", "created_at"),
    )

    id: Mapped[int] = pk_column()
    event_key: Mapped[str] = mapped_column(String(191), nullable=False)
    outbox_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("email_outbox.id", name="fk_mail_delivery_event_outbox", ondelete="SET NULL"),
    )
    ticket_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_tickets.id", name="fk_mail_delivery_event_ticket", ondelete="SET NULL"),
    )
    source_email_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("emails.id", name="fk_mail_delivery_event_source_email", ondelete="SET NULL"),
    )
    original_message_id: Mapped[str | None] = mapped_column(String(500))
    final_recipient: Mapped[str | None] = mapped_column(String(500))
    delivery_status: Mapped[str] = mapped_column(String(30), nullable=False)
    action: Mapped[str | None] = mapped_column(String(30))
    smtp_status_code: Mapped[str | None] = mapped_column(String(30))
    diagnostic_code: Mapped[str | None] = mapped_column(String(1000))
    evidence: Mapped[dict | None] = mapped_column(mysql.JSON)
    occurred_at: Mapped[datetime | None] = datetime_column()

    outbox: Mapped[EmailOutbox | None] = relationship(EmailOutbox, foreign_keys=[outbox_id], lazy="raise")
