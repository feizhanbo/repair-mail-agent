from __future__ import annotations

import os

import pytest

from app.config import settings
from tools.audit_langgraph_release import email_dispatch_probe


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_EMAIL_DISPATCH_INTEGRATION_TESTS") != "1",
    reason="MySQL email dispatch concurrency test is explicitly opt-in",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_mysql_concurrent_email_dispatch_queues_exactly_one_graph_start() -> None:
    report = await email_dispatch_probe(settings.DB_SMOKE_DATABASE_URL.strip())

    assert report["connected"] is True
    assert report["passed"] is True
    assert report["checks"] == {
        "second_dispatch_blocked_on_email_lock": True,
        "second_dispatch_reused_owner": True,
        "same_execution_id": True,
        "same_job_id": True,
        "single_graph_start_row": True,
        "failed_recovery_reused_job": True,
        "failed_recovery_kept_execution_identity": True,
    }
