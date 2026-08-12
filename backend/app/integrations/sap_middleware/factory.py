from __future__ import annotations

from app.config import settings
from app.integrations.sap_middleware.contracts import SapMiddlewareAdapter


def create_sap_middleware_adapter() -> SapMiddlewareAdapter:
    adapter = settings.RELAY_ADAPTER.strip().lower()
    if adapter == "test_http":
        from app.integrations.sap_middleware.test_http import TestHttpSapMiddlewareAdapter

        return TestHttpSapMiddlewareAdapter()
    if adapter == "sqlserver":
        from app.integrations.sap_middleware.sqlserver import SqlServerSapMiddlewareAdapter

        return SqlServerSapMiddlewareAdapter()
    from app.integrations.sap_middleware.sqlserver import SqlServerSapMiddlewareAdapter

    return SqlServerSapMiddlewareAdapter(invalid_adapter=adapter)
