from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main
from app.models import JobRunLog
from app.services import jobs
from app.services.jobs import JobLeaseLost
from app.services.common import utcnow


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _running_job(*, owner_token: str = "worker:token") -> JobRunLog:
    return JobRunLog(
        id=41,
        job_name="graph_start",
        job_type="graph_start",
        status="running",
        locked_by=owner_token,
        attempt_count=1,
        max_attempts=3,
        processed_count=0,
        success_count=0,
        failed_count=0,
    )


def test_owner_tokens_are_unique_bounded_and_traceable() -> None:
    first = jobs._new_job_owner_token("worker-a")
    second = jobs._new_job_owner_token("worker-a")

    assert first != second
    assert first.startswith("worker-a:")
    assert len(first) <= 100


@pytest.mark.anyio
@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
async def test_renew_job_lease_requires_matching_live_owner(rowcount: int, expected: bool) -> None:
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(rowcount=rowcount)))

    assert await jobs.renew_job_lease(session, job_id=41, owner_token="worker:token") is expected
    session.execute.assert_awaited_once()


@pytest.mark.anyio
async def test_lock_owned_job_rejects_superseded_owner() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=_running_job(owner_token="new-owner")))

    with pytest.raises(JobLeaseLost, match="JOB_LEASE_LOST"):
        await jobs._lock_owned_job(session, job_id=41, owner_token="old-owner")


@pytest.mark.anyio
async def test_stale_graph_job_requires_manual_recovery_instead_of_automatic_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _running_job()
    job.locked_at = utcnow() - timedelta(hours=1)
    job.started_at = job.locked_at

    class Rows:
        def scalars(self):
            return self

        def all(self):
            return [job]

    monkeypatch.setattr(jobs.settings, "ASYNC_JOB_STALE_SECONDS", 30)
    session = SimpleNamespace(execute=AsyncMock(return_value=Rows()))

    assert await jobs.recover_stale_jobs(session) == 1
    assert job.status == "needs_manual_review"
    assert job.error_code == "GRAPH_JOB_LEASE_EXPIRED_UNCERTAIN"
    assert job.next_run_at is None
    assert job.locked_at is None
    assert job.locked_by is None


@pytest.mark.anyio
async def test_stale_graph_job_retry_requires_operator_fencing_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _running_job()
    job.status = "needs_manual_review"
    job.error_code = "GRAPH_JOB_LEASE_EXPIRED_UNCERTAIN"
    job.error_message = "uncertain"
    job.locked_at = None
    job.locked_by = None
    job.finished_at = utcnow()
    job.metadata_json = {"execution_id": "exec-1"}
    session = SimpleNamespace(get=AsyncMock(return_value=job))
    log_event = AsyncMock()
    monkeypatch.setattr(jobs, "log_system_event", log_event)

    with pytest.raises(ValueError, match="GRAPH_PREVIOUS_WORKER_STOP_CONFIRMATION_REQUIRED"):
        await jobs.reactivate_stale_graph_job(
            session,
            job_id=41,
            operator_user_id=7,
            confirm_previous_worker_stopped=False,
        )
    session.get.assert_not_awaited()

    recovered = await jobs.reactivate_stale_graph_job(
        session,
        job_id=41,
        operator_user_id=7,
        confirm_previous_worker_stopped=True,
        reason="worker pod terminated",
    )

    assert recovered is job
    assert job.status == "queued"
    assert job.error_code is None
    assert job.max_attempts == 3
    assert job.metadata_json["lease_recovery"]["confirmed_by_user_id"] == 7
    assert job.metadata_json["lease_recovery"]["reason"]["redacted"] is True
    assert "worker pod terminated" not in str(job.metadata_json)
    log_event.assert_awaited_once()


@pytest.mark.anyio
async def test_failed_graph_start_recovery_requeues_same_execution(monkeypatch) -> None:
    job = _running_job()
    job.status = "failed"
    job.attempt_count = 3
    job.max_attempts = 3
    job.finished_at = utcnow()
    job.error_code = "WORKFLOW_CHECKPOINT_MISSING"
    job.error_message = "failed"
    job.locked_at = None
    job.locked_by = None
    job.metadata_json = {"execution_id": "email-7-job-41"}
    execution = SimpleNamespace(
        execution_mode="langgraph",
        email_id=7,
        trigger_job_id=41,
        status="failed",
    )
    job.resource_type = "email"
    job.resource_id = 7
    session = SimpleNamespace(get=AsyncMock(return_value=job), scalar=AsyncMock(return_value=execution))
    log_event = AsyncMock()
    monkeypatch.setattr(jobs, "log_system_event", log_event)

    recovered = await jobs.reactivate_failed_graph_start_job(
        session,
        job_id=41,
        operator_user_id=7,
        reason="checkpoint repaired",
    )

    assert recovered is job
    assert job.status == "queued"
    assert job.max_attempts == 4
    assert job.metadata_json["execution_id"] == "email-7-job-41"
    assert job.metadata_json["terminal_recovery"]["requested_by_user_id"] == 7
    assert job.metadata_json["terminal_recovery"]["reason"]["redacted"] is True
    log_event.assert_awaited_once()


@pytest.mark.anyio
async def test_failed_graph_start_recovery_rejects_other_terminal_jobs() -> None:
    job = _running_job()
    job.job_type = "graph_resume"
    job.status = "failed"
    session = SimpleNamespace(get=AsyncMock(return_value=job))

    with pytest.raises(ValueError, match="FAILED_GRAPH_START_JOB_NOT_RECOVERABLE"):
        await jobs.reactivate_failed_graph_start_job(
            session,
            job_id=41,
            operator_user_id=7,
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "execution",
    [
        SimpleNamespace(
            execution_mode="shadow",
            email_id=7,
            trigger_job_id=41,
            status="failed",
        ),
        SimpleNamespace(
            execution_mode="langgraph",
            email_id=8,
            trigger_job_id=41,
            status="failed",
        ),
        SimpleNamespace(
            execution_mode="langgraph",
            email_id=7,
            trigger_job_id=42,
            status="failed",
        ),
        SimpleNamespace(
            execution_mode="langgraph",
            email_id=7,
            trigger_job_id=41,
            status="completed",
        ),
    ],
)
async def test_failed_graph_start_recovery_fails_closed_on_execution_conflict(execution) -> None:
    job = _running_job()
    job.status = "failed"
    job.resource_type = "email"
    job.resource_id = 7
    job.metadata_json = {"execution_id": "email-7-job-41"}
    session = SimpleNamespace(get=AsyncMock(return_value=job), scalar=AsyncMock(return_value=execution))

    with pytest.raises(ValueError, match="FAILED_GRAPH_START_EXECUTION_CONFLICT"):
        await jobs.reactivate_failed_graph_start_job(
            session,
            job_id=41,
            operator_user_id=7,
        )


@pytest.mark.anyio
async def test_execute_claimed_job_rejects_stale_token_before_business_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = AsyncMock()
    monkeypatch.setattr(jobs, "_execute_job_command", command)
    session = SimpleNamespace(rollback=AsyncMock())

    with pytest.raises(JobLeaseLost, match="JOB_LEASE_LOST_BEFORE_EXECUTION"):
        await jobs.execute_claimed_job(
            session,
            _running_job(owner_token="new-owner"),
            expected_owner_token="old-owner",
        )

    command.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.anyio
async def test_execute_claimed_job_does_not_finalize_after_lease_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _running_job()
    session = SimpleNamespace(rollback=AsyncMock())
    monkeypatch.setattr(jobs, "_execute_job_command", AsyncMock(return_value={"status": "completed"}))
    monkeypatch.setattr(
        jobs,
        "_lock_owned_job",
        AsyncMock(side_effect=JobLeaseLost("JOB_LEASE_LOST")),
    )
    log_event = AsyncMock()
    monkeypatch.setattr(jobs, "log_system_event", log_event)

    with pytest.raises(JobLeaseLost, match="JOB_LEASE_LOST"):
        await jobs.execute_claimed_job(session, job)

    session.rollback.assert_awaited_once()
    log_event.assert_not_awaited()
    assert job.status == "running"
    assert job.locked_by == "worker:token"


def test_heartbeat_interval_keeps_three_chances_inside_stale_window() -> None:
    assert main._job_lease_heartbeat_interval(30) == 10
    assert main._job_lease_heartbeat_interval(90) == 30
    assert main._job_lease_heartbeat_interval(900) == 30


@pytest.mark.anyio
async def test_heartbeat_marks_lease_lost_when_owner_no_longer_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace(commit=AsyncMock())

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(main, "AsyncSessionLocal", lambda: SessionContext())
    monkeypatch.setattr(main, "renew_job_lease", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "_job_lease_heartbeat_interval", lambda _stale: 0)
    stop = asyncio.Event()
    lease_lost = asyncio.Event()

    await main._job_lease_heartbeat(
        job_id=41,
        owner_token="worker:token",
        stop=stop,
        lease_lost=lease_lost,
    )

    assert lease_lost.is_set()


@pytest.mark.anyio
async def test_worker_cancels_business_execution_when_heartbeat_loses_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    async def lose_lease(*, lease_lost: asyncio.Event, **_kwargs) -> None:
        lease_lost.set()

    async def long_execution(*_args, **_kwargs) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(main, "_job_lease_heartbeat", lose_lease)
    monkeypatch.setattr(main, "execute_claimed_job", long_execution)
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    completed = await main._execute_job_with_lease(
        session,
        _running_job(),
        owner_token="worker:token",
    )

    assert completed is False
    assert cancelled.is_set()
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_worker_commits_only_after_execution_finishes_with_live_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def live_heartbeat(*, stop: asyncio.Event, **_kwargs) -> None:
        await stop.wait()

    monkeypatch.setattr(main, "_job_lease_heartbeat", live_heartbeat)
    monkeypatch.setattr(main, "execute_claimed_job", AsyncMock(return_value=_running_job()))
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    completed = await main._execute_job_with_lease(
        session,
        _running_job(),
        owner_token="worker:token",
    )

    assert completed is True
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
