from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


def relay_configured() -> bool:
    return bool(settings.RELAY_BASE_URL and settings.RELAY_API_KEY)


async def sync_sn_assets_from_relay(
    session: AsyncSession,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    del session, user_id
    if not settings.RELAY_SN_SYNC_ENABLED:
        return {"status": "disabled", "configured": relay_configured(), "synced_count": 0}
    if not relay_configured():
        return {"status": "not_configured", "configured": False, "synced_count": 0}
    return {
        "status": "not_implemented",
        "configured": True,
        "synced_count": 0,
        "message": "Relay SN sync contract is reserved; endpoint/auth/schema are required before real sync.",
    }


async def push_ai_parse_result_to_relay(
    session: AsyncSession,
    *,
    parse_result_id: int,
    user_id: int | None = None,
) -> dict[str, Any]:
    del session, user_id
    if not settings.RELAY_PUSH_ENABLED:
        return {"status": "disabled", "configured": relay_configured(), "parse_result_id": parse_result_id}
    if not relay_configured():
        return {"status": "not_configured", "configured": False, "parse_result_id": parse_result_id}
    return {
        "status": "not_implemented",
        "configured": True,
        "parse_result_id": parse_result_id,
        "message": "Relay parse-result push contract is reserved; endpoint/auth/schema are required before real push.",
    }
