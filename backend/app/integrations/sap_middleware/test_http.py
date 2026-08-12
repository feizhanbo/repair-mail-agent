from __future__ import annotations

from typing import Sequence
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.config import settings
from app.integrations.sap_middleware.contracts import (
    ConnectionHealth,
    ExternalRmaResult,
    ExternalRmaSubmissionItem,
    ExternalSnRecord,
    SapMiddlewareConfigurationError,
    SapTransactionError,
)
from app.services.mail_safety import test_mail_configuration_reasons as _test_mail_configuration_reasons


class TestHttpSapMiddlewareAdapter:
    def _missing(self) -> list[str]:
        parsed = urlsplit(settings.TEST_RELAY_BASE_URL)
        missing: list[str] = []
        if not settings.RELAY_SQLSERVER_ENABLED:
            missing.append("RELAY_SQLSERVER_ENABLED")
        if settings.APP_ENV.lower() not in {"dev", "test"}:
            missing.append("TEST_RELAY_ENV_NOT_ALLOWED")
        if not settings.RUN_REAL_MAIL_INTEGRATION_TESTS:
            missing.append("RUN_REAL_MAIL_INTEGRATION_TESTS")
        if _test_mail_configuration_reasons():
            missing.append("TEST_MAIL_GATE_FAILED")
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            missing.append("TEST_RELAY_LOOPBACK_URL_REQUIRED")
        if not settings.TEST_RELAY_TOKEN:
            missing.append("TEST_RELAY_TOKEN")
        return sorted(set(missing))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.TEST_RELAY_TOKEN}"}

    async def check_connection(self) -> ConnectionHealth:
        missing = self._missing()
        if missing:
            return ConnectionHealth(False, "misconfigured", tuple(missing), {"adapter": "test_http"})
        try:
            async with httpx.AsyncClient(timeout=settings.RELAY_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{settings.TEST_RELAY_BASE_URL.rstrip('/')}/health", headers=self._headers()
                )
                response.raise_for_status()
        except Exception as exc:
            return ConnectionHealth(False, "unreachable", details={"error": type(exc).__name__})
        return ConnectionHealth(True, "configured", details={"adapter": "test_http"})

    async def fetch_all_sn_records(self) -> Sequence[ExternalSnRecord]:
        raise SapMiddlewareConfigurationError("TEST_RELAY_HAS_NO_SN_MASTER")

    async def submit_rma_batch(self, items: Sequence[ExternalRmaSubmissionItem]) -> None:
        health = await self.check_connection()
        if not health.configured:
            raise SapMiddlewareConfigurationError("TEST_RELAY_NOT_CONFIGURED:" + ",".join(health.missing))
        payload = [
            {**item.payload, "source_request_id": str(item.source_request_id), "sn": item.sn}
            for item in items
        ]
        try:
            async with httpx.AsyncClient(timeout=settings.RELAY_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{settings.TEST_RELAY_BASE_URL.rstrip('/')}/records/batch",
                    json={"items": payload},
                    headers=self._headers(),
                )
                response.raise_for_status()
        except Exception as exc:
            raise SapTransactionError("TEST_RELAY_BATCH_SUBMIT_FAILED") from exc

    async def find_records_by_source_request_ids(
        self, source_request_ids: Sequence[UUID]
    ) -> Sequence[ExternalRmaResult]:
        if not source_request_ids:
            return []
        try:
            async with httpx.AsyncClient(timeout=settings.RELAY_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{settings.TEST_RELAY_BASE_URL.rstrip('/')}/records/query",
                    json={"source_request_ids": [str(value) for value in source_request_ids]},
                    headers=self._headers(),
                )
                response.raise_for_status()
                rows = response.json().get("items", [])
        except Exception as exc:
            raise SapTransactionError("TEST_RELAY_RESULT_QUERY_FAILED") from exc
        return [
            ExternalRmaResult(
                source_request_id=UUID(str(row["source_request_id"])),
                sn=row.get("sn"),
                rma_no=str(row["rma_no"]).strip() if row.get("rma_no") else None,
                raw_data=row,
            )
            for row in rows
        ]
