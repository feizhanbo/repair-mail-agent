from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, TimestampMixin, created_at_column, datetime_column, pk_column


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uk_users_username"),
        UniqueConstraint("email", name="uk_users_email"),
        Index("idx_users_status", "status"),
    )

    id: Mapped[int] = pk_column()
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    real_name: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    department: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    last_login_at: Mapped[datetime | None] = datetime_column()


class Role(TimestampMixin, Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("role_code", name="uk_roles_code"),)

    id: Mapped[int] = pk_column()
    role_code: Mapped[str] = mapped_column(String(50), nullable=False)
    role_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))


class UserRole(CreatedAtMixin, Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uk_user_roles_user_role"),
        Index("idx_user_roles_user", "user_id"),
        Index("idx_user_roles_role", "role_id"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("users.id", name="fk_user_roles_user"), nullable=False)
    role_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), ForeignKey("roles.id", name="fk_user_roles_role"), nullable=False)

