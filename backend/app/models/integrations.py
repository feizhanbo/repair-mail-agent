from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, datetime_column, pk_column


class ExternalSyncCheckpoint(TimestampMixin, Base):
    __tablename__ = "external_sync_checkpoints"
    __table_args__ = (UniqueConstraint("sync_name", name="uk_external_sync_checkpoints_name"),)

    id: Mapped[int] = pk_column()
    sync_name: Mapped[str] = mapped_column(String(100), nullable=False)
    cursor_value: Mapped[str | None] = mapped_column(String(500))
    last_full_sync_at: Mapped[datetime | None] = datetime_column()
    last_success_at: Mapped[datetime | None] = datetime_column()
    last_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="never_run")
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    statistics_json: Mapped[dict | None] = mapped_column(mysql.JSON)


class TicketRelayExport(TimestampMixin, Base):
    __tablename__ = "ticket_relay_exports"
    __table_args__ = (
        UniqueConstraint("ticket_id", "ticket_version", "payload_hash", name="uk_ticket_relay_export_snapshot"),
        Index("idx_ticket_relay_export_status", "status", "next_retry_at"),
        Index("idx_ticket_relay_export_ticket", "ticket_id", "created_at"),
    )

    id: Mapped[int] = pk_column()
    ticket_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_tickets.id", name="fk_ticket_relay_export_ticket", ondelete="CASCADE"),
        nullable=False,
    )
    ticket_version: Mapped[int] = mapped_column(nullable=False)
    payload_hash: Mapped[str] = mapped_column(mysql.CHAR(64), nullable=False)
    payload_snapshot: Mapped[dict] = mapped_column(mysql.JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    remote_record_key: Mapped[str | None] = mapped_column(String(191))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = datetime_column()
    exported_at: Mapped[datetime | None] = datetime_column()
