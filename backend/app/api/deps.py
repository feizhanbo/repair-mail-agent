from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import TokenDecodeError, decode_access_token
from app.models import Role, User, UserRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    real_name: str
    email: str | None
    roles: list[str]


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CurrentUser:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="AUTH_INVALID_TOKEN",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        user_id = int(payload.get("sub", "0"))
    except (TokenDecodeError, TypeError, ValueError):
        raise credentials_error

    result = await session.execute(select(User).where(User.id == user_id, User.status == "active"))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_error

    roles_result = await session.execute(
        select(Role.role_code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id)
        .order_by(Role.role_code)
    )
    roles = list(roles_result.scalars().all())
    return CurrentUser(id=user.id, username=user.username, real_name=user.real_name, email=user.email, roles=roles)


def require_roles(*allowed_roles: str):
    async def dependency(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if "admin" in current_user.roles or any(role in current_user.roles for role in allowed_roles):
            return current_user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")

    return dependency
