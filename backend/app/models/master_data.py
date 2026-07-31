from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, bool_column, datetime_column, pk_column


class SnAsset(TimestampMixin, Base):
    __tablename__ = "sn_assets"
    __table_args__ = (
        UniqueConstraint("sn", name="uk_sn_assets_sn"),
        Index("idx_sn_assets_customer_code", "customer_code"),
        Index("idx_sn_assets_customer_name", "customer_name"),
        Index("idx_sn_assets_material_code", "material_code"),
        Index("idx_sn_assets_material_name", "material_name"),
        Index("idx_sn_assets_service_tracking_card", "service_tracking_card_no"),
        Index("idx_sn_assets_parent_sn", "parent_sn"),
        Index("idx_sn_assets_top_sn", "top_sn"),
        Index("idx_sn_assets_status", "asset_status"),
        Index("idx_sn_assets_source", "source_file_hash", "source_row_no"),
        Index("idx_sn_assets_external", "source_system", "external_id"),
        Index("idx_sn_assets_source_updated", "source_system", "source_updated_at"),
    )

    id: Mapped[int] = pk_column()
    customer_code: Mapped[str] = mapped_column(String(50), nullable=False)
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    material_code: Mapped[str] = mapped_column(String(100), nullable=False)
    material_name: Mapped[str | None] = mapped_column(String(255))
    sn: Mapped[str] = mapped_column(String(100), nullable=False)
    service_tracking_card_no: Mapped[str | None] = mapped_column(String(100))
    parent_sn: Mapped[str | None] = mapped_column(String(100))
    top_sn: Mapped[str | None] = mapped_column(String(100))
    parent_material_code: Mapped[str | None] = mapped_column(String(100))
    top_material_code: Mapped[str | None] = mapped_column(String(100))
    asset_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="valid")
    warranty_start_date: Mapped[date | None] = mapped_column(mysql.DATE)
    warranty_end_date: Mapped[date | None] = mapped_column(mysql.DATE)
    source_file_name: Mapped[str | None] = mapped_column(String(255))
    source_file_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    source_row_no: Mapped[int | None] = mapped_column()
    raw_data: Mapped[dict | None] = mapped_column(mysql.JSON)
    source_system: Mapped[str] = mapped_column(String(30), nullable=False, server_default="local")
    external_id: Mapped[str | None] = mapped_column(String(191))
    source_updated_at: Mapped[datetime | None] = datetime_column()
    imported_by_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_sn_assets_imported_by"))
    imported_at: Mapped[datetime | None] = datetime_column()


class BoardCard(TimestampMixin, Base):
    __tablename__ = "board_cards"
    __table_args__ = (
        Index("idx_board_cards_board_code", "board_code"),
        Index("idx_board_cards_board_name", "board_name"),
        Index("idx_board_cards_route", "customer_scope", "route_type", "status"),
        Index("idx_board_cards_location", "return_location", "status"),
        Index("idx_board_cards_material_name", "material_name"),
        Index("idx_board_cards_ship_to_beijing", "need_ship_to_beijing"),
        Index("idx_board_cards_status", "status"),
        Index("idx_board_cards_source", "source_file_hash", "source_row_no"),
    )

    id: Mapped[int] = pk_column()
    board_code: Mapped[str] = mapped_column(String(100), nullable=False)
    board_name: Mapped[str | None] = mapped_column(String(255))
    return_location: Mapped[str] = mapped_column(String(20), nullable=False)
    route_type: Mapped[str] = mapped_column(String(30), nullable=False, server_default="board_rule")
    customer_scope: Mapped[str] = mapped_column(String(20), nullable=False, server_default="domestic")
    # Compatibility columns. New business logic must use the explicit board/route
    # fields above; these columns remain for one migration window.
    material_code: Mapped[str] = mapped_column(String(100), nullable=False)
    material_name: Mapped[str | None] = mapped_column(String(255))
    need_ship_to_beijing: Mapped[bool] = bool_column(False)
    shipping_address: Mapped[str | None] = mapped_column(String(500))
    shipping_contact: Mapped[str | None] = mapped_column(String(100))
    shipping_phone: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    source_file_name: Mapped[str | None] = mapped_column(String(255))
    source_file_hash: Mapped[str | None] = mapped_column(mysql.CHAR(64))
    source_row_no: Mapped[int | None] = mapped_column()
    raw_data: Mapped[dict | None] = mapped_column(mysql.JSON)
    imported_by_user_id: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_board_cards_imported_by"))
    imported_at: Mapped[datetime | None] = datetime_column()


class CustomerServicePolicy(TimestampMixin, Base):
    __tablename__ = "customer_service_policies"
    __table_args__ = (
        UniqueConstraint("policy_code", name="uk_customer_service_policies_code"),
        Index("idx_customer_service_policies_customer", "customer_code", "enabled"),
        Index("idx_customer_service_policies_type", "policy_type", "enabled"),
        Index("idx_customer_service_policies_effective", "effective_from", "effective_until"),
    )

    id: Mapped[int] = pk_column()
    policy_code: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_code: Mapped[str] = mapped_column(String(50), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(255))
    policy_type: Mapped[str] = mapped_column(String(30), nullable=False)
    charge_status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="manual_confirmation"
    )
    customer_scope: Mapped[str | None] = mapped_column(String(20))
    effective_from: Mapped[date | None] = mapped_column(mysql.DATE)
    effective_until: Mapped[date | None] = mapped_column(mysql.DATE)
    repair_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, server_default="0")
    currency: Mapped[str] = mapped_column(String(10), nullable=False, server_default="CNY")
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False, server_default="13")
    shipping_fee_text: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        server_default="one-way charge/单次收费",
    )
    reply_salutation: Mapped[str | None] = mapped_column(String(100))
    hide_company_name: Mapped[bool] = bool_column(False)
    force_manual_review: Mapped[bool] = bool_column(False)
    enabled: Mapped[bool] = bool_column(True)
    source_file_name: Mapped[str | None] = mapped_column(String(255))
    source_row_no: Mapped[int | None] = mapped_column()
    imported_by_user_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("users.id", name="fk_customer_service_policies_imported_by"),
    )
    imported_at: Mapped[datetime | None] = datetime_column()

