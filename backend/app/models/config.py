from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_column


class SystemConfig(TimestampMixin, Base):
    __tablename__ = "system_configs"
    __table_args__ = (
        UniqueConstraint("config_key", name="uk_system_configs_key"),
        Index("idx_system_configs_group", "config_group"),
    )

    id: Mapped[int] = pk_column()
    config_key: Mapped[str] = mapped_column(String(100), nullable=False)
    config_group: Mapped[str] = mapped_column(String(50), nullable=False)
    value_type: Mapped[str] = mapped_column(String(20), nullable=False)
    config_value: Mapped[object] = mapped_column(mysql.JSON, nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, server_default="1")
    updated_by_user_id: Mapped[int | None] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("users.id", name="fk_system_configs_updated_by"),
    )
