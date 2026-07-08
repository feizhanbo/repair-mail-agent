from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok, page
from app.schemas.business import UserCreateRequest, UserResetPasswordRequest, UserRolesRequest, UserStatusRequest, UserUpdateRequest
from app.services import users as user_service

router = APIRouter()


@router.get("")
async def list_users(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    page_no: int = Query(1, alias="page", ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    keyword: str | None = None,
    username: str | None = None,
    real_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    role: str | None = None,
) -> dict:
    del current_user
    items, total = await user_service.list_users(
        session,
        page=page_no,
        page_size=page_size,
        status_filter=status_filter,
        keyword=keyword,
        username=username,
        real_name=real_name,
        email=email,
        phone=phone,
        role=role,
    )
    return page(items, total, page_no, page_size)


@router.post("")
async def create_user(
    payload: UserCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    result = await user_service.create_user(session, values=payload.model_dump(), operator_user_id=current_user.id)
    return ok(result, "user created")


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    result = await user_service.update_user(
        session,
        user_id=user_id,
        values=payload.model_dump(exclude_unset=True),
        operator_user_id=current_user.id,
    )
    return ok(result, "user updated")


@router.patch("/{user_id}/status")
async def update_user_status(
    user_id: int,
    payload: UserStatusRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    result = await user_service.update_user_status(session, user_id=user_id, user_status=payload.status, operator_user_id=current_user.id)
    return ok(result, "user status updated")


@router.put("/{user_id}/roles")
async def update_user_roles(
    user_id: int,
    payload: UserRolesRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    user = await user_service.get_user(session, user_id)
    result = await user_service.set_user_roles(session, user=user, roles=payload.roles, operator_user_id=current_user.id)
    await session.commit()
    return ok(result, "user roles updated")


@router.post("/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    payload: UserResetPasswordRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    result = await user_service.reset_user_password(session, user_id=user_id, password=payload.password, operator_user_id=current_user.id)
    return ok(result, "user password reset")


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    if user_id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="USER_CANNOT_DELETE_SELF")
    result = await user_service.delete_user(session, user_id=user_id, operator_user_id=current_user.id)
    return ok(result, "user deleted")
