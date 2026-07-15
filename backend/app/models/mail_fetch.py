from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, datetime_column, pk_column


class MailFetchRecord(CreatedAtMixin, Base):
    __tablename__ = "mail_fetch_records"
    __table_args__ = (
        UniqueConstraint("mailbox_account", "folder_name", "imap_uid", name="uk_mail_fetch_records"),
        Index("idx_mail_fetch_records_message_id", "message_id"),
        Index("idx_mail_fetch_records_job", "fetch_job_run_id"),
        Index("idx_mail_fetch_records_retry", "fetch_status", "next_retry_at"),
    )

    id: Mapped[int] = pk_column()
    mailbox_account: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_uid: Mapped[str] = mapped_column(String(100), nullable=False)
    message_id: Mapped[str] = mapped_column(String(500), nullable=False)
    fetch_job_run_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("job_run_logs.id", name="fk_mail_fetch_records_job"))
    email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_mail_fetch_records_email"))
    duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    fetch_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="success")
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="1")
    last_attempt_at: Mapped[datetime | None] = datetime_column()
    next_retry_at: Mapped[datetime | None] = datetime_column()
    error_message: Mapped[str | None] = mapped_column(Text)
