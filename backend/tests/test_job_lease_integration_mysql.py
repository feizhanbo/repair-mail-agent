from __future__ import annotations

import os

import pytest

from app.config import settings
from tools.audit_langgraph_release import job_lease_probe


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_JOB_LEASE_INTEGRATION_TESTS") != "1",
    reason="MySQL job lease integration test is explicitly opt-in",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_mysql_job_lease_fences_old_owner_and_requires_explicit_graph_recovery() -> None:
    report = await job_lease_probe(settings.DB_SMOKE_DATABASE_URL.strip())

    assert report["connected"] is True
    assert report["passed"] is True
    assert report["checks"] == {
        "first_owner_renewed": True,
        "wrong_owner_rejected": True,
        "single_stale_recovered": True,
        "graph_stale_failed_closed": True,
        "operator_recovery_queued": True,
        "owner_token_rotated": True,
        "old_owner_rejected": True,
        "new_owner_renewed": True,
    }
