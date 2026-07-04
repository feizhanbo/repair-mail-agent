from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.config import settings
from app.core.database import get_session
from app.core.response import ok
from app.core.security import create_access_token, verify_password
from app.models import Role, User, UserRole
from app.schemas.business import LoginRequest
from app.services.audit import log_operation
from app.services.common import utcnow

router = APIRouter()


@router.post("/login")
async def login(payload: LoginRequest, session: Annotated[AsyncSession, Depends(get_session)]) -> dict:
    result = await session.execute(select(User).where(User.username == payload.username, User.status == "active"))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="AUTH_INVALID_CREDENTIALS")

    roles_result = await session.execute(
        select(Role.role_code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id)
        .order_by(Role.role_code)
    )
    roles = list(roles_result.scalars().all())
    user.last_login_at = utcnow()
    await log_operation(
        session,
        user_id=user.id,
        operation_type="auth_login",
        target_type="user",
        target_id=user.id,
        description="用户登录。",
    )
    await session.commit()
    return ok(
        {
            "access_token": create_access_token(user.id, roles),
            "token_type": "bearer",
            "expires_in": settings.JWT_EXPIRE_MINUTES * 60,
            "user": {
                "id": user.id,
                "username": user.username,
                "real_name": user.real_name,
                "email": user.email,
                "roles": roles,
            },
        }
    )


@router.get("/me")
async def me(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> dict:
    return ok(
        {
            "user": {
                "id": current_user.id,
                "username": current_user.username,
                "real_name": current_user.real_name,
                "email": current_user.email,
            },
            "roles": current_user.roles,
        }
    )

