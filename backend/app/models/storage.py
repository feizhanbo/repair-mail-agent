from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, pk_column


class OssObject(CreatedAtMixin, Base):
    __tablename__ = "oss_objects"
    __table_args__ = (
        UniqueConstraint("bucket", "object_key", name="uk_oss_objects_bucket_key"),
        Index("idx_oss_objects_hash", "sha256_hash"),
        Index("idx_oss_objects_source", "source_type", "created_at"),
        Index("idx_oss_objects_status", "upload_status"),
    )

    id: Mapped[int] = pk_column()
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(String(640), nullable=False)
    object_version: Mapped[str | None] = mapped_column(String(128))
    original_file_name: Mapped[str | None] = mapped_column(String(255))
    safe_file_name: Mapped[str | None] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    sha256_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    etag: Mapped[str | None] = mapped_column(String(128))
    storage_class: Mapped[str | None] = mapped_column(String(30), server_default="Standard")
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    upload_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_oss_objects_created_by"))

    raw_emails: Mapped[list["Email"]] = relationship(
        "Email", foreign_keys="Email.raw_eml_oss_object_id", back_populates="raw_eml_oss_object", lazy="raise"
    )
    attachments: Mapped[list["EmailAttachment"]] = relationship(
        "EmailAttachment", foreign_keys="EmailAttachment.oss_object_id", back_populates="oss_object", lazy="raise"
    )
    reply_pdf_records: Mapped[list["ReplyRecord"]] = relationship(
        "ReplyRecord", foreign_keys="ReplyRecord.rma_pdf_oss_object_id", back_populates="rma_pdf_oss_object", lazy="raise"
    )
    ticket_rmas: Mapped[list["TicketRma"]] = relationship(
        "TicketRma", foreign_keys="TicketRma.pdf_oss_object_id", back_populates="pdf_oss_object", lazy="raise"
    )
    input_jobs: Mapped[list["JobRunLog"]] = relationship(
        "JobRunLog", foreign_keys="JobRunLog.input_oss_object_id", back_populates="input_oss_object", lazy="raise"
    )
    output_jobs: Mapped[list["JobRunLog"]] = relationship(
        "JobRunLog", foreign_keys="JobRunLog.output_oss_object_id", back_populates="output_oss_object", lazy="raise"
    )
