from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, datetime_column, pk_column


class MailFetchRecord(CreatedAtMixin, Base):
    __tablename__ = "mail_fetch_records"
    __table_args__ = (
        UniqueConstraint("mailbox_account", "folder_name", "uid_validity", "imap_uid", name="uk_mail_fetch_records"),
        Index("idx_mail_fetch_records_message_id", "message_id"),
        Index("idx_mail_fetch_records_job", "fetch_job_run_id"),
        Index("idx_mail_fetch_records_retry", "fetch_status", "next_retry_at"),
        Index("idx_mail_fetch_records_stage", "processing_stage", "created_at"),
        Index("idx_mail_fetch_records_classification", "handling_level", "intent_type"),
        Index("idx_mail_fetch_records_thread", "thread_id"),
    )

    id: Mapped[int] = pk_column()
    mailbox_account: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    uid_validity: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), nullable=False, server_default="0")
    imap_uid: Mapped[str] = mapped_column(String(100), nullable=False)
    message_id: Mapped[str] = mapped_column(String(500), nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(500))
    references_header: Mapped[str | None] = mapped_column(Text)
    fetch_job_run_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("job_run_logs.id", name="fk_mail_fetch_records_job"))
    email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_mail_fetch_records_email"))
    thread_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("email_threads.id", name="fk_mail_fetch_records_thread"))
    duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    fetch_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="success")
    processing_stage: Mapped[str] = mapped_column(String(50), nullable=False, server_default="discovered")
    recovery_stage: Mapped[str | None] = mapped_column(String(50))
    intent_type: Mapped[str | None] = mapped_column(String(50))
    handling_level: Mapped[str | None] = mapped_column(String(30))
    classification_version: Mapped[str | None] = mapped_column(String(50))
    classification_confidence: Mapped[object | None] = mapped_column(mysql.DECIMAL(5, 4))
    classification_reason_code: Mapped[str | None] = mapped_column(String(100))
    classification_evidence: Mapped[dict | None] = mapped_column(mysql.JSON)
    classified_at: Mapped[datetime | None] = datetime_column()
    completed_at: Mapped[datetime | None] = datetime_column()
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="1")
    last_attempt_at: Mapped[datetime | None] = datetime_column()
    next_retry_at: Mapped[datetime | None] = datetime_column()
    error_message: Mapped[str | None] = mapped_column(Text)

    thread: Mapped["EmailThread | None"] = relationship("EmailThread", foreign_keys=[thread_id], lazy="raise")
    ai_call_logs: Mapped[list["AiCallLog"]] = relationship(
        "AiCallLog", foreign_keys="AiCallLog.mail_fetch_record_id", back_populates="mail_fetch_record", lazy="raise"
    )
