from __future__ import annotations

from datetime import datetime

from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    ticket: Mapped["RepairTicket"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="relay_exports", lazy="raise"
    )
    sap_lines: Mapped[list["ExportSap"]] = relationship(
        "ExportSap", foreign_keys="ExportSap.relay_export_id", back_populates="relay_export", passive_deletes=True, lazy="raise"
    )


class ExportSap(TimestampMixin, Base):
    __tablename__ = "export_sap"
    __table_args__ = (
        UniqueConstraint("submission_key", name="uk_export_sap_submission_key"),
        UniqueConstraint("remote_call_id", name="uk_export_sap_remote_call_id"),
        UniqueConstraint(
            "ticket_item_id",
            "ticket_version",
            "payload_hash",
            name="uk_export_sap_item_snapshot",
        ),
        Index("idx_export_sap_sn", "sn"),
        Index("idx_export_sap_customer_code", "customer_code"),
        Index("idx_export_sap_material_code", "material_code"),
        Index("idx_export_sap_ticket", "ticket_id", "created_at"),
        Index("idx_export_sap_relay", "relay_export_id", "status"),
        Index("idx_export_sap_status_retry", "status", "next_retry_at"),
        Index("idx_export_sap_rma", "rma_no"),
    )

    id: Mapped[int] = pk_column()
    ticket_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_tickets.id", name="fk_export_sap_ticket", ondelete="CASCADE"),
        nullable=False,
    )
    ticket_item_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_ticket_items.id", name="fk_export_sap_ticket_item", ondelete="CASCADE"),
        nullable=False,
    )
    relay_export_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("ticket_relay_exports.id", name="fk_export_sap_relay_export", ondelete="CASCADE"),
        nullable=False,
    )
    ticket_version: Mapped[int] = mapped_column(nullable=False)
    submission_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(mysql.CHAR(64), nullable=False)
    policy_snapshot: Mapped[dict | None] = mapped_column(mysql.JSON)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    remote_call_id: Mapped[str | None] = mapped_column(String(191))
    rma_no: Mapped[str | None] = mapped_column(String(30))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = datetime_column()
    submitted_at: Mapped[datetime | None] = datetime_column()
    accepted_at: Mapped[datetime | None] = datetime_column()
    last_polled_at: Mapped[datetime | None] = datetime_column()
    rma_received_at: Mapped[datetime | None] = datetime_column()
    sn: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_code: Mapped[str | None] = mapped_column(String(50))
    material_code: Mapped[str | None] = mapped_column(String(100))
    customer_name: Mapped[str | None] = mapped_column(String(255))
    material_name: Mapped[str | None] = mapped_column(String(255))
    charge_status: Mapped[str | None] = mapped_column(String(30))
    contact_person: Mapped[str | None] = mapped_column(String(100))
    contact_phone: Mapped[str | None] = mapped_column(String(100))
    email_subject: Mapped[str | None] = mapped_column(String(500))
    problem_description: Mapped[str | None] = mapped_column(Text)
    repair_requested_at: Mapped[datetime | None] = datetime_column()
    mailing_address: Mapped[str | None] = mapped_column(String(500))
    currency: Mapped[str | None] = mapped_column(String(10))
    shipping_fee: Mapped[str | None] = mapped_column(String(100))
    repair_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))

    ticket: Mapped["RepairTicket"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="sap_exports", lazy="raise"
    )
    ticket_item: Mapped["RepairTicketItem"] = relationship(
        "RepairTicketItem", foreign_keys=[ticket_item_id], back_populates="sap_exports", lazy="raise"
    )
    relay_export: Mapped[TicketRelayExport] = relationship(
        TicketRelayExport, foreign_keys=[relay_export_id], back_populates="sap_lines", lazy="raise"
    )
    external_operations: Mapped[list["ExternalOperationRecord"]] = relationship(
        "ExternalOperationRecord", foreign_keys="ExternalOperationRecord.export_sap_id", back_populates="export_sap", passive_deletes=True, lazy="raise"
    )


class TicketRma(TimestampMixin, Base):
    __tablename__ = "ticket_rmas"
    __table_args__ = (
        UniqueConstraint("rma_no", name="uk_ticket_rmas_no"),
        Index("idx_ticket_rmas_ticket", "ticket_id", "created_at"),
        Index("idx_ticket_rmas_status", "status", "updated_at"),
    )

    id: Mapped[int] = pk_column()
    ticket_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_tickets.id", name="fk_ticket_rmas_ticket", ondelete="CASCADE"),
        nullable=False,
    )
    rma_no: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="received")
    policy_snapshot: Mapped[dict | None] = mapped_column(mysql.JSON)
    pdf_oss_object_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("oss_objects.id", name="fk_ticket_rmas_pdf"),
    )
    pdf_sha256: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    pdf_validation_status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="pending"
    )
    pdf_archive_status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="pending"
    )
    reply_record_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("reply_records.id", name="fk_ticket_rmas_reply"),
    )
    received_at: Mapped[datetime | None] = datetime_column()
    sent_at: Mapped[datetime | None] = datetime_column()
    pdf_archived_at: Mapped[datetime | None] = datetime_column()
    issued_at: Mapped[datetime | None] = datetime_column()

    ticket: Mapped["RepairTicket"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="rmas", lazy="raise"
    )
    pdf_oss_object: Mapped["OssObject | None"] = relationship(
        "OssObject", foreign_keys=[pdf_oss_object_id], back_populates="ticket_rmas", lazy="raise"
    )
    reply_record: Mapped["ReplyRecord | None"] = relationship(
        "ReplyRecord", foreign_keys=[reply_record_id], back_populates="ticket_rmas", lazy="raise"
    )
    items: Mapped[list["TicketRmaItem"]] = relationship(
        "TicketRmaItem", foreign_keys="TicketRmaItem.ticket_rma_id", back_populates="ticket_rma", passive_deletes=True, lazy="raise"
    )


class TicketRmaItem(TimestampMixin, Base):
    __tablename__ = "ticket_rma_items"
    __table_args__ = (
        UniqueConstraint("ticket_item_id", name="uk_ticket_rma_items_ticket_item"),
        Index("idx_ticket_rma_items_rma", "ticket_rma_id"),
    )

    id: Mapped[int] = pk_column()
    ticket_rma_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("ticket_rmas.id", name="fk_ticket_rma_items_rma", ondelete="CASCADE"),
        nullable=False,
    )
    ticket_item_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_ticket_items.id", name="fk_ticket_rma_items_item", ondelete="CASCADE"),
        nullable=False,
    )

    ticket_rma: Mapped[TicketRma] = relationship(
        TicketRma, foreign_keys=[ticket_rma_id], back_populates="items", lazy="raise"
    )
    ticket_item: Mapped["RepairTicketItem"] = relationship(
        "RepairTicketItem", foreign_keys=[ticket_item_id], back_populates="rma_item", lazy="raise"
    )


class ExternalOperationRecord(TimestampMixin, Base):
    __tablename__ = "external_operation_records"
    __table_args__ = (
        UniqueConstraint(
            "operation_type",
            "operation_key",
            name="uk_external_operation_type_key",
        ),
        Index("idx_external_operation_status_retry", "status", "next_retry_at"),
        Index("idx_external_operation_ticket", "ticket_id", "created_at"),
        Index("idx_external_operation_email", "email_id", "created_at"),
    )

    id: Mapped[int] = pk_column()
    operation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(191), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="planned")
    ticket_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("repair_tickets.id", name="fk_external_operations_ticket", ondelete="CASCADE"),
    )
    email_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("emails.id", name="fk_external_operations_email", ondelete="SET NULL"),
    )
    reply_record_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("reply_records.id", name="fk_external_operations_reply", ondelete="CASCADE"),
    )
    export_sap_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("export_sap.id", name="fk_external_operations_export_sap", ondelete="CASCADE"),
    )
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    remote_reference: Mapped[str | None] = mapped_column(String(500))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(
        mysql.TINYINT(display_width=1), nullable=False, server_default="1"
    )
    recovery_stage: Mapped[str | None] = mapped_column(String(100))
    next_retry_at: Mapped[datetime | None] = datetime_column()
    started_at: Mapped[datetime | None] = datetime_column()
    completed_at: Mapped[datetime | None] = datetime_column()
    details_json: Mapped[dict | None] = mapped_column(mysql.JSON)

    ticket: Mapped["RepairTicket | None"] = relationship(
        "RepairTicket", foreign_keys=[ticket_id], back_populates="external_operations", lazy="raise"
    )
    email: Mapped["Email | None"] = relationship(
        "Email", foreign_keys=[email_id], back_populates="external_operations", lazy="raise"
    )
    reply_record: Mapped["ReplyRecord | None"] = relationship(
        "ReplyRecord", foreign_keys=[reply_record_id], back_populates="external_operations", lazy="raise"
    )
    export_sap: Mapped[ExportSap | None] = relationship(
        ExportSap, foreign_keys=[export_sap_id], back_populates="external_operations", lazy="raise"
    )
