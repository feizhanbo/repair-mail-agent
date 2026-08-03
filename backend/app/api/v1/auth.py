from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ACTIVE_ROLE_CODES, CurrentUser, get_current_user
from app.config import settings
from app.core.database import get_session
from app.core.response import ok
from app.core.security import create_access_token, verify_password
from app.models import Role, User, UserRole
from app.schemas.business import LoginRequest, PasswordChangeRequest, ProfileUpdateRequest
from app.services.audit import log_operation
from app.services.common import utcnow
from app.services import users as user_service

router = APIRouter()


@router.post("/login")
async def login(
    payload: LoginRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
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
    roles = [role for role in roles_result.scalars().all() if role in ACTIVE_ROLE_CODES]
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
    access_token = create_access_token(user.id, roles)
    response.set_cookie(
        key="repair_mail_session",
        value=access_token,
        max_age=settings.JWT_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.APP_ENV.lower() in {"prod", "production"},
        samesite="strict",
        path="/api/v1",
    )
    return ok(
        {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.JWT_EXPIRE_MINUTES * 60,
            "user": {
                "id": user.id,
                "username": user.username,
                "real_name": user.real_name,
                "email": user.email,
                "phone": user.phone,
                "status": user.status,
                "roles": roles,
            },
        }
    )


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(
        key="repair_mail_session",
        path="/api/v1",
        secure=settings.APP_ENV.lower() in {"prod", "production"},
        httponly=True,
        samesite="strict",
    )
    return ok({}, "logged out")


@router.get("/me")
async def me(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> dict:
    return ok(
        {
            "user": {
                "id": current_user.id,
                "username": current_user.username,
                "real_name": current_user.real_name,
                "email": current_user.email,
                "phone": current_user.phone,
                "status": current_user.status,
            },
            "roles": current_user.roles,
        }
    )


@router.patch("/me/profile")
async def update_profile(
    payload: ProfileUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await user_service.update_profile(session, user_id=current_user.id, values=payload.model_dump(exclude_unset=True))
    return ok(result, "profile updated")


@router.patch("/me/password")
async def change_password(
    payload: PasswordChangeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> dict:
    result = await user_service.change_password(
        session,
        user_id=current_user.id,
        old_password=payload.old_password,
        new_password=payload.new_password,
    )
    return ok(result, "password changed")

