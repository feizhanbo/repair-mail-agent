"""Compatibility facade for the SAP middleware integration.

New code must depend on ``app.integrations.sap_middleware`` or the focused
business services. No SQL Server connection or SQL belongs in this module.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.sap_middleware import (
    SapMiddlewareConfigurationError,
    SapMiddlewareError,
    SapUnknownCommitStateError,
)
from app.services.sap_sn_sync import create_sn_sync_batch


RelayConfigurationError = SapMiddlewareConfigurationError
RelayOperationError = SapMiddlewareError
RelaySubmissionUncertainError = SapUnknownCommitStateError


def relay_configuration_status() -> dict[str, Any]:
    if not settings.RELAY_SQLSERVER_ENABLED:
        return {"adapter": settings.RELAY_ADAPTER, "status": "disabled", "configured": False, "missing": []}
    adapter = settings.RELAY_ADAPTER.strip().lower()
    if adapter == "test_http":
        missing = []
        if not settings.TEST_RELAY_BASE_URL:
            missing.append("TEST_RELAY_BASE_URL")
        if not settings.TEST_RELAY_TOKEN:
            missing.append("TEST_RELAY_TOKEN")
    elif adapter == "sqlserver":
        required = {
            "RELAY_SQLSERVER_HOST": settings.RELAY_SQLSERVER_HOST,
            "RELAY_SQLSERVER_DATABASE": settings.RELAY_SQLSERVER_DATABASE,
            "RELAY_SQLSERVER_USER": settings.RELAY_SQLSERVER_USER,
            "RELAY_SQLSERVER_PASSWORD": settings.RELAY_SQLSERVER_PASSWORD,
            "RELAY_SQLSERVER_DRIVER": settings.RELAY_SQLSERVER_DRIVER,
            "RELAY_SQLSERVER_SN_TABLE": settings.RELAY_SQLSERVER_SN_TABLE,
            "RELAY_SQLSERVER_REQUEST_TABLE": settings.RELAY_SQLSERVER_REQUEST_TABLE,
            "RELAY_SQLSERVER_RESULT_TABLE": settings.RELAY_SQLSERVER_RESULT_TABLE,
            "RELAY_SQLSERVER_REQUEST_ID_COLUMN": settings.RELAY_SQLSERVER_REQUEST_ID_COLUMN,
            "RELAY_SQLSERVER_RMA_COLUMN": settings.RELAY_SQLSERVER_RMA_COLUMN,
        }
        missing = [name for name, value in required.items() if not value]
    else:
        missing = ["RELAY_ADAPTER_INVALID"]
    return {
        "adapter": adapter,
        "status": "misconfigured" if missing else "configured",
        "configured": not missing,
        "missing": sorted(set(missing)),
    }


def relay_configured() -> bool:
    return bool(relay_configuration_status()["configured"])


async def sync_sn_assets_from_relay(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    full: bool = True,
) -> dict[str, Any]:
    del full
    if not settings.RELAY_SQLSERVER_ENABLED:
        return {"status": "disabled", "configured": False, "synced_count": 0}
    result = await create_sn_sync_batch(session, user_id=user_id)
    return {**result, "configured": True, "synced_count": result.get("source_count", 0), "full": True}


async def push_ai_parse_result_to_relay(
    session: AsyncSession,
    *,
    parse_result_id: int,
    user_id: int | None = None,
) -> dict[str, Any]:
    del session, user_id
    return {
        "status": "deprecated",
        "parse_result_id": parse_result_id,
        "message": "Use validated RequestID ticket batch export",
    }
