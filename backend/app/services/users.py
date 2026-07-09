from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.models import (
    BoardCard,
    EmailTicketLink,
    FieldAuditLog,
    ManualReviewTask,
    NotificationEvent,
    OperationLog,
    OssObject,
    ParseResult,
    RepairTicket,
    ReplyRecord,
    ReplyTemplate,
    Role,
    SnAsset,
    TicketStatusLog,
    User,
    UserRole,
)
from app.services.audit import log_operation
from app.services.common import model_to_dict, paginate_scalars

ROLE_CODES = ("admin", "supervisor", "operator")
USER_FIELDS = (
    "id",
    "username",
    "real_name",
    "email",
    "phone",
    "status",
    "last_login_at",
    "created_at",
    "updated_at",
)

USER_REFERENCE_FIELDS = (
    ("operation_logs", "user_id", OperationLog.user_id),
    ("email_ticket_links", "linked_by_user_id", EmailTicketLink.linked_by_user_id),
    ("sn_assets", "imported_by_user_id", SnAsset.imported_by_user_id),
    ("board_cards", "imported_by_user_id", BoardCard.imported_by_user_id),
    ("parse_results", "applied_by_user_id", ParseResult.applied_by_user_id),
    ("parse_results", "accepted_by_user_id", ParseResult.accepted_by_user_id),
    ("reply_templates", "created_by_user_id", ReplyTemplate.created_by_user_id),
    ("reply_records", "reviewed_by_user_id", ReplyRecord.reviewed_by_user_id),
    ("manual_review_tasks", "assigned_user_id", ManualReviewTask.assigned_user_id),
    ("manual_review_tasks", "claimed_by_user_id", ManualReviewTask.claimed_by_user_id),
    ("manual_review_tasks", "resolved_by_user_id", ManualReviewTask.resolved_by_user_id),
    ("notification_events", "recipient_user_id", NotificationEvent.recipient_user_id),
    ("oss_objects", "created_by_user_id", OssObject.created_by_user_id),
    ("repair_tickets", "assigned_user_id", RepairTicket.assigned_user_id),
    ("ticket_status_logs", "operator_user_id", TicketStatusLog.operator_user_id),
    ("field_audit_logs", "operator_user_id", FieldAuditLog.operator_user_id),
)


async def _roles_for_user(session: AsyncSession, user_id: int) -> list[str]:
    result = await session.execute(
        select(Role.role_code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
        .order_by(Role.role_code)
    )
    return list(result.scalars().all())


async def _role_map(session: AsyncSession) -> dict[str, Role]:
    result = await session.execute(select(Role).where(Role.role_code.in_(ROLE_CODES)))
    roles = {role.role_code: role for role in result.scalars().all()}
    missing = [code for code in ROLE_CODES if code not in roles]
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"ROLE_NOT_INITIALIZED:{','.join(missing)}")
    return roles


def serialize_user(user: User, roles: list[str]) -> dict[str, Any]:
    data = model_to_dict(user, USER_FIELDS)
    data["roles"] = roles
    return data


async def get_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="USER_NOT_FOUND")
    return user


async def get_user_detail(session: AsyncSession, user_id: int) -> dict[str, Any]:
    user = await get_user(session, user_id)
    return serialize_user(user, await _roles_for_user(session, user.id))


async def list_users(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    status_filter: str | None = None,
    keyword: str | None = None,
    username: str | None = None,
    real_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    role: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    statement = select(User)
    if status_filter:
        statement = statement.where(User.status == status_filter)
    if username:
        statement = statement.where(User.username.like(f"%{username}%"))
    if real_name:
        statement = statement.where(User.real_name.like(f"%{real_name}%"))
    if email:
        statement = statement.where(User.email.like(f"%{email}%"))
    if phone:
        statement = statement.where(User.phone.like(f"%{phone}%"))
    if role:
        statement = statement.where(
            User.id.in_(
                select(UserRole.user_id)
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.role_code == role)
            )
        )
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(
            (User.username.like(like))
            | (User.real_name.like(like))
            | (User.email.like(like))
            | (User.phone.like(like))
        )
    statement = statement.order_by(User.updated_at.desc(), User.id.desc())
    users, total = await paginate_scalars(session, statement, page, page_size)
    items = [serialize_user(user, await _roles_for_user(session, user.id)) for user in users]
    return items, total


async def _ensure_unique_user(session: AsyncSession, *, username: str | None = None, email: str | None = None, exclude_user_id: int | None = None) -> None:
    if username:
        statement = select(User).where(User.username == username)
        if exclude_user_id:
            statement = statement.where(User.id != exclude_user_id)
        if await session.scalar(statement):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="USER_USERNAME_EXISTS")
    if email:
        statement = select(User).where(User.email == email)
        if exclude_user_id:
            statement = statement.where(User.id != exclude_user_id)
        if await session.scalar(statement):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="USER_EMAIL_EXISTS")


async def set_user_roles(session: AsyncSession, *, user: User, roles: list[str], operator_user_id: int | None = None) -> dict[str, Any]:
    invalid = [role for role in roles if role not in ROLE_CODES]
    if invalid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"ROLE_NOT_ALLOWED:{','.join(invalid)}")
    role_map = await _role_map(session)
    await session.execute(delete(UserRole).where(UserRole.user_id == user.id))
    for role_code in roles:
        session.add(UserRole(user_id=user.id, role_id=role_map[role_code].id))
    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="user_roles_updated",
        target_type="user",
        target_id=user.id,
        after_data={"roles": roles},
    )
    await session.flush()
    await session.refresh(user)
    return serialize_user(user, roles)


async def create_user(session: AsyncSession, *, values: dict[str, Any], operator_user_id: int) -> dict[str, Any]:
    await _ensure_unique_user(session, username=values["username"], email=values.get("email"))
    roles = values.pop("roles", [])
    password = values.pop("password")
    user = User(password_hash=hash_password(password), **values)
    session.add(user)
    await session.flush()
    await set_user_roles(session, user=user, roles=roles, operator_user_id=operator_user_id)
    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="user_created",
        target_type="user",
        target_id=user.id,
        after_data={key: value for key, value in values.items() if key != "password_hash"},
    )
    await session.commit()
    return await get_user_detail(session, user.id)


async def update_user(session: AsyncSession, *, user_id: int, values: dict[str, Any], operator_user_id: int) -> dict[str, Any]:
    user = await get_user(session, user_id)
    values = {key: value for key, value in values.items() if value is not None}
    await _ensure_unique_user(session, email=values.get("email"), exclude_user_id=user.id)
    before = {key: getattr(user, key) for key in values}
    for key, value in values.items():
        setattr(user, key, value)
    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="user_updated",
        target_type="user",
        target_id=user.id,
        before_data=before,
        after_data=values,
    )
    await session.commit()
    return await get_user_detail(session, user.id)


async def update_user_status(session: AsyncSession, *, user_id: int, user_status: str, operator_user_id: int) -> dict[str, Any]:
    user = await get_user(session, user_id)
    old_status = user.status
    user.status = user_status
    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="user_status_updated",
        target_type="user",
        target_id=user.id,
        before_data={"status": old_status},
        after_data={"status": user_status},
    )
    await session.commit()
    return await get_user_detail(session, user.id)


async def reset_user_password(session: AsyncSession, *, user_id: int, password: str, operator_user_id: int) -> dict[str, Any]:
    if not password.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="USER_PASSWORD_REQUIRED")
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="USER_PASSWORD_TOO_LONG")
    user = await get_user(session, user_id)
    user.password_hash = hash_password(password)
    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="user_password_reset",
        target_type="user",
        target_id=user.id,
    )
    await session.commit()
    return await get_user_detail(session, user.id)


async def _user_reference_summary(session: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for table_name, column_name, column in USER_REFERENCE_FIELDS:
        count = await session.scalar(select(func.count()).select_from(column.class_).where(column == user_id))
        if count:
            references.append({"table": table_name, "field": column_name, "count": int(count)})
    return references


async def delete_user(session: AsyncSession, *, user_id: int, operator_user_id: int) -> dict[str, Any]:
    user = await get_user(session, user_id)
    if user.id == operator_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="USER_CANNOT_DELETE_SELF")

    references = await _user_reference_summary(session, user.id)
    if references:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "USER_HAS_REFERENCES", "data": {"references": references}},
        )

    roles = await _roles_for_user(session, user.id)
    deleted_user = serialize_user(user, roles)
    await session.execute(delete(UserRole).where(UserRole.user_id == user.id))
    await session.delete(user)
    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="user_deleted",
        target_type="user",
        target_id=user_id,
        before_data=deleted_user,
        after_data={"deleted": True},
    )
    await session.commit()
    return {"deleted": True, "user": deleted_user}


async def update_profile(session: AsyncSession, *, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
    user = await get_user(session, user_id)
    values = {key: value for key, value in values.items() if value is not None}
    await _ensure_unique_user(session, email=values.get("email"), exclude_user_id=user.id)
    before = {key: getattr(user, key) for key in values}
    for key, value in values.items():
        setattr(user, key, value)
    await log_operation(
        session,
        user_id=user.id,
        operation_type="profile_updated",
        target_type="user",
        target_id=user.id,
        before_data=before,
        after_data=values,
    )
    await session.commit()
    return await get_user_detail(session, user.id)


async def change_password(session: AsyncSession, *, user_id: int, old_password: str, new_password: str) -> dict[str, Any]:
    user = await get_user(session, user_id)
    if not verify_password(old_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="USER_OLD_PASSWORD_INVALID")
    user.password_hash = hash_password(new_password)
    await log_operation(
        session,
        user_id=user.id,
        operation_type="profile_password_changed",
        target_type="user",
        target_id=user.id,
    )
    await session.commit()
    return await get_user_detail(session, user.id)
