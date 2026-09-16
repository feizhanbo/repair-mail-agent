from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.request_context import get_client_ip, set_user_id
from app.core.security import TokenDecodeError, decode_access_token
from app.models import Role, User, UserRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)
ACTIVE_ROLE_CODES = frozenset({"admin", "operator"})
logger = logging.getLogger(__name__)
_invalid_token_samples: dict[str, float] = {}


def _log_invalid_token(reason: str) -> None:
    now = time.monotonic()
    key = f"{get_client_ip() or 'unknown'}:{reason}"
    last = _invalid_token_samples.get(key, 0.0)
    if now - last < 60:
        return
    if len(_invalid_token_samples) >= 1024:
        cutoff = now - 60
        for sample_key, timestamp in list(_invalid_token_samples.items()):
            if timestamp < cutoff:
                _invalid_token_samples.pop(sample_key, None)
        if len(_invalid_token_samples) >= 1024:
            _invalid_token_samples.pop(next(iter(_invalid_token_samples)))
    _invalid_token_samples[key] = now
    logger.warning(
        "Authentication token rejected",
        extra={"event": "auth_token_invalid", "reason_code": reason},
    )


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    real_name: str
    email: str | None
    phone: str | None
    status: str
    roles: list[str]
    department: str | None = None


async def get_current_user(
    request: Request,
    token: Annotated[str | None, Depends(oauth2_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CurrentUser:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="AUTH_INVALID_TOKEN",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        session_token = token or request.cookies.get("repair_mail_session")
        if not session_token:
            raise TokenDecodeError("missing token")
        payload = decode_access_token(session_token)
        user_id = int(payload.get("sub", "0"))
    except (TokenDecodeError, TypeError, ValueError):
        _log_invalid_token("INVALID_OR_EXPIRED")
        raise credentials_error

    result = await session.execute(select(User).where(User.id == user_id, User.status == "active"))
    user = result.scalar_one_or_none()
    if user is None:
        _log_invalid_token("USER_INACTIVE_OR_MISSING")
        raise credentials_error

    roles_result = await session.execute(
        select(Role.role_code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id)
        .order_by(Role.role_code)
    )
    roles = [role for role in roles_result.scalars().all() if role in ACTIVE_ROLE_CODES]
    set_user_id(user.id)
    return CurrentUser(
        id=user.id,
        username=user.username,
        real_name=user.real_name,
        email=user.email,
        phone=user.phone,
        status=user.status,
        roles=roles,
    )


def require_roles(*allowed_roles: str):
    async def dependency(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if "admin" in current_user.roles or any(role in current_user.roles for role in allowed_roles):
            return current_user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AUTH_FORBIDDEN")

    return dependency
