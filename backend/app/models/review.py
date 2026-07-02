from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, TimestampMixin, datetime_column, pk_column


class ManualReviewTask(TimestampMixin, Base):
    __tablename__ = "manual_review_tasks"
    __table_args__ = (
        Index("idx_manual_tasks_ticket", "ticket_id"),
        Index("idx_manual_tasks_email", "email_id"),
        Index("idx_manual_tasks_status", "status"),
        Index("idx_manual_tasks_queue", "status", "priority", "created_at"),
        Index("idx_manual_tasks_assignee", "assigned_user_id", "status"),
    )

    id: Mapped[int] = pk_column()
    ticket_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("repair_tickets.id", name="fk_manual_tasks_ticket", ondelete="CASCADE"), nullable=False)
    email_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("emails.id", name="fk_manual_tasks_email"))
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, server_default="normal")
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    description: Mapped[str | None] = mapped_column(Text)
    trigger_reason: Mapped[str | None] = mapped_column(String(500))
    assigned_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_manual_tasks_assignee"))
    claimed_by_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_manual_tasks_claimed_by"))
    claimed_at: Mapped[datetime | None] = datetime_column()
    resolved_by_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_manual_tasks_resolver"))
    resolved_at: Mapped[datetime | None] = datetime_column()
    resolution: Mapped[str | None] = mapped_column(Text)


class NotificationEvent(CreatedAtMixin, Base):
    __tablename__ = "notification_events"
    __table_args__ = (
        Index("idx_notifications_recipient", "recipient_user_id", "delivery_status", "created_at"),
        Index("idx_notifications_role", "recipient_role_code", "delivery_status", "created_at"),
        Index("idx_notifications_target", "target_type", "target_id"),
        Index("idx_notifications_event", "event_type", "created_at"),
    )

    id: Mapped[int] = pk_column()
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str | None] = mapped_column(String(1000))
    priority: Mapped[str] = mapped_column(String(20), nullable=False, server_default="normal")
    recipient_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_notifications_recipient_user"))
    recipient_role_code: Mapped[str | None] = mapped_column(String(50))
    delivery_channel: Mapped[str] = mapped_column(String(30), nullable=False, server_default="in_app")
    delivery_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    read_at: Mapped[datetime | None] = datetime_column()
    metadata_json: Mapped[dict | None] = mapped_column("metadata", mysql.JSON)
    delivered_at: Mapped[datetime | None] = datetime_column()

