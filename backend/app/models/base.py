from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import MetaData, text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # MySQL does not return server defaults on INSERT. Load them inside flush so
    # async callers never trigger implicit database I/O during serialization.
    __mapper_args__ = {"eager_defaults": True}


def pk_column() -> Any:
    return mapped_column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)


def bigint_fk(nullable: bool = True) -> Any:
    return mapped_column(mysql.BIGINT(unsigned=True), nullable=nullable)


def bool_column(default: bool = False, nullable: bool = False) -> Any:
    return mapped_column(mysql.TINYINT(display_width=1), nullable=nullable, server_default=text("1" if default else "0"))


def datetime_column(nullable: bool = True) -> Any:
    return mapped_column(mysql.DATETIME(fsp=3), nullable=nullable)


def created_at_column() -> Any:
    return mapped_column(mysql.DATETIME(fsp=3), nullable=False, server_default=text("CURRENT_TIMESTAMP(3)"))


def updated_at_column() -> Any:
    return mapped_column(
        mysql.DATETIME(fsp=3),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=text("CURRENT_TIMESTAMP(3)"),
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = created_at_column()


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = updated_at_column()

