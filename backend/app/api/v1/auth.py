from __future__ import annotations

from fastapi import APIRouter

from app.core.response import ok

router = APIRouter()


@router.post("/login")
async def login_placeholder() -> dict:
    return ok({"access_token": "", "token_type": "bearer"}, "auth module placeholder")


@router.get("/me")
async def me_placeholder() -> dict:
    return ok({"user": None, "roles": []}, "auth module placeholder")

