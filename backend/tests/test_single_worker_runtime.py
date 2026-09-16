from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from app.services import jobs
from app.services.job_dispatcher import (
    JOB_HANDLERS,
    JobOutcome,
    JobOutcomeKind,
    normalize_job_outcome,
    validate_job_handlers,
)
from app.models import Base, JobRunLog, ManualReviewTask, WorkerLease
from app.services.common import utcnow
from app.services.notification_task_repair import repair_notification_and_task_data
from app.services.worker_lease import WorkerLeaseUnavailable, acquire_worker_lease
from app.services.worker_fencing import JobOwnershipLost


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent


def test_dispatcher_covers_every_supported_job_type() -> None:
    validate_job_handlers(jobs.JOB_TYPES)
    assert set(JOB_HANDLERS) == jobs.JOB_TYPES


def test_api_entrypoint_contains_no_background_runtime() -> None:
    source = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    assert "claim_next_job" not in source
    assert "AsyncIOScheduler" not in source
    assert "_scheduled_job_worker" not in source


def test_worker_has_one_process_entry_loop_and_required_coroutines() -> None:
    source = (BACKEND_DIR / "app" / "worker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    asyncio_runs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr == "run"
    ]
    assert len(asyncio_runs) == 1
    async_functions = {
        node.name for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
    }
    assert {
        "worker_main",
        "scheduler_loop",
        "consumer_loop",
        "watchdog_loop",
        "worker_heartbeat_loop",
    } <= async_functions
    assert "asyncio.create_subprocess_exec" in source
    assert '"-m",\n            "app.worker_executor"' in source


def test_compose_exposes_only_unified_business_worker() -> None:
    compose = (PROJECT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    assert "airma-worker:" in compose
    assert "app.worker" in compose
    assert "mail-worker:" not in compose
    assert "app.mail_worker" not in compose
    assert "stop_grace_period: 45s" in compose


def test_worker_has_out_of_event_loop_stall_guard_and_immediate_job_heartbeat_abort() -> None:
    source = (BACKEND_DIR / "app" / "worker.py").read_text(encoding="utf-8")
    assert "class EventLoopWatchdog" in source
    assert "threading.Thread" in source
    assert "os._exit" in source
    assert "{process_wait, heartbeat}" in source
    assert "JOB_HEARTBEAT_OWNERSHIP_LOST" in source
    assert source.index("event_loop_watchdog.start()") < source.index(
        "await assert_schema_current(session)"
    )


def test_event_loop_watchdog_forces_process_exit_when_pulse_is_stale() -> None:
    from app.worker import EventLoopWatchdog

    exits: list[int] = []
    watchdog = EventLoopWatchdog(timeout_seconds=10, exit_func=exits.append)
    watchdog._last_pulse = 0
    watchdog._stop = SimpleNamespace(wait=lambda _seconds: False)
    watchdog._run()
    assert exits == [70]


class _SupervisorTestProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._finished: asyncio.Event | None = None

    async def wait(self) -> int:
        if self._finished is None:
            self._finished = asyncio.Event()
        await self._finished.wait()
        return int(self.returncode or 0)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        if self._finished is not None:
            self._finished.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        if self._finished is not None:
            self._finished.set()


class _SupervisorTestSession:
    def __init__(self, job: JobRunLog) -> None:
        self.job = job

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _model, _identity, **_kwargs):
        return self.job

    async def commit(self):
        return None


def test_supervisor_hard_timeout_terminates_executor_and_records_interruption(monkeypatch) -> None:
    from app import worker
    from app.services.worker_lease import ActiveWorkerLease

    job = JobRunLog(id=51, job_type="ai_log_maintenance", status="running")
    process = _SupervisorTestProcess()
    interruptions: list[str] = []

    async def no_lease_check(*_args, **_kwargs):
        return None

    async def create_process(*_args, **_kwargs):
        return process

    async def mark_interrupted(*_args, **kwargs):
        interruptions.append(kwargs["error_code"])
        return True

    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: _SupervisorTestSession(job))
    monkeypatch.setattr(worker, "assert_worker_lease", no_lease_check)
    monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(worker, "mark_interrupted_job", mark_interrupted)
    monkeypatch.setitem(worker.JOB_TIMEOUT_SECONDS, "ai_log_maintenance", 0.01)
    lease = ActiveWorkerLease(1, "test", "default", "worker-test", 4)

    asyncio.run(worker._execute_one(51, lease))

    assert process.terminated is True
    assert interruptions == ["JOB_EXECUTION_TIMEOUT"]


def test_supervisor_job_heartbeat_failure_aborts_executor_immediately(monkeypatch) -> None:
    from app import worker
    from app.services.worker_lease import ActiveWorkerLease

    job = JobRunLog(id=52, job_type="ai_log_maintenance", status="running")
    process = _SupervisorTestProcess()

    async def no_lease_check(*_args, **_kwargs):
        return None

    async def create_process(*_args, **_kwargs):
        return process

    async def failed_heartbeat(*_args, **_kwargs):
        raise RuntimeError("FAULT_INJECTED_HEARTBEAT_FAILURE")

    async def must_not_mark(*_args, **_kwargs):
        raise AssertionError("lost-heartbeat supervisor must not write stale job state")

    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: _SupervisorTestSession(job))
    monkeypatch.setattr(worker, "assert_worker_lease", no_lease_check)
    monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(worker, "_job_heartbeat_loop", failed_heartbeat)
    monkeypatch.setattr(worker, "mark_interrupted_job", must_not_mark)
    lease = ActiveWorkerLease(1, "test", "default", "worker-test", 4)

    with pytest.raises(RuntimeError, match="FAULT_INJECTED_HEARTBEAT_FAILURE"):
        asyncio.run(worker._execute_one(52, lease))

    assert process.terminated is True


def test_sn_sync_is_a_bounded_persistent_phase_machine() -> None:
    source = (BACKEND_DIR / "app" / "services" / "sap_sn_sync.py").read_text(
        encoding="utf-8"
    )
    adapter_source = (
        BACKEND_DIR / "app" / "integrations" / "sap_middleware" / "sqlserver.py"
    ).read_text(encoding="utf-8")
    assert "advance_sn_sync_job" in source
    for phase in ('"stage"', '"validate"', '"apply"', '"finalize"'):
        assert phase in source
    assert ".limit(SN_SYNC_CHUNK_SIZE)" in source
    assert "fetch_sn_records_page" in adapter_source
    assert "fetch_all_sn_records" not in adapter_source


def test_database_pool_is_bounded_and_checks_stale_connections() -> None:
    source = (BACKEND_DIR / "app" / "core" / "database.py").read_text(encoding="utf-8")
    assert "pool_pre_ping=True" in source
    assert "pool_size=settings.DB_POOL_SIZE" in source
    assert "max_overflow=settings.DB_MAX_OVERFLOW" in source
    assert "pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS" in source
    assert "pool_recycle=settings.DB_POOL_RECYCLE_SECONDS" in source
    from app.core.database import engine

    assert engine.sync_engine.dialect._send_false_to_ping is True


def test_deploy_stops_legacy_worker_and_migrates_before_restart() -> None:
    deploy = (PROJECT_DIR / "deploy.sh").read_text(encoding="utf-8")
    stop_old = deploy.index("docker stop repair-mail-worker")
    migrate = deploy.index("alembic upgrade head")
    restart = deploy.index("docker compose up -d --remove-orphans", migrate)
    assert stop_old < migrate < restart
    assert "mysqldump" in deploy
    assert deploy.index("mysqldump") < migrate
    assert 'APP_ENV=production' in deploy
    assert 'current_branch" != "main"' in deploy
    assert "EXPECTED_COMMIT_SHA" in deploy
    assert "actual_commit_sha" in deploy
    assert "backups/" in (PROJECT_DIR / ".gitignore").read_text(encoding="utf-8")
    assert "restore_services_on_failure" in deploy
    assert "maintenance_started=1" in deploy


def test_claim_records_worker_fencing_token(monkeypatch) -> None:
    now = jobs.utcnow()
    job = SimpleNamespace(
        status="queued",
        started_at=None,
        locked_at=None,
        locked_by=None,
        fencing_token=None,
        job_type="smtp_send",
        attempt_count=0,
        max_attempts=3,
        next_run_at=now,
        execution_deadline_at=None,
    )

    class Session:
        async def scalar(self, _statement):
            return job

        async def flush(self):
            return None

    async def no_recovery(_session):
        return 0

    monkeypatch.setattr(jobs, "recover_stale_jobs", no_recovery)
    claimed = asyncio.run(
        jobs.claim_next_job(
            Session(),
            worker_id="airma-worker:test:1",
            fencing_token=7,
        )
    )
    assert claimed is job
    assert job.status == "running"
    assert job.locked_by == "airma-worker:test:1"
    assert job.fencing_token == 7
    assert job.attempt_count == 1


def test_second_worker_cannot_take_an_active_lease() -> None:
    row = WorkerLease(
        id=1,
        environment="production",
        queue_name="default",
        instance_id="worker-v1",
        app_version="v1",
        fencing_token=3,
        heartbeat_at=utcnow(),
        lease_expires_at=utcnow() + timedelta(minutes=1),
    )

    class Session:
        async def scalar(self, _statement):
            return row

    async def acquire():
        return await acquire_worker_lease(
            Session(),
            environment="production",
            queue_name="default",
            instance_id="worker-v2",
            app_version="v2",
            lease_seconds=60,
        )

    try:
        asyncio.run(acquire())
    except WorkerLeaseUnavailable:
        pass
    else:
        raise AssertionError("a second worker acquired an active lease")


def test_expired_lease_takeover_increments_fencing_token() -> None:
    row = WorkerLease(
        id=1,
        environment="production",
        queue_name="default",
        instance_id="worker-v1",
        app_version="v1",
        fencing_token=3,
        heartbeat_at=utcnow() - timedelta(minutes=2),
        lease_expires_at=utcnow() - timedelta(minutes=1),
    )

    class Session:
        async def scalar(self, _statement):
            return row

        async def flush(self):
            return None

    lease = asyncio.run(
        acquire_worker_lease(
            Session(),
            environment="production",
            queue_name="default",
            instance_id="worker-v2",
            app_version="v2",
            lease_seconds=60,
        )
    )
    assert lease.fencing_token == 4
    assert row.instance_id == "worker-v2"


def test_unknown_external_job_outcome_fails_closed() -> None:
    job = JobRunLog(job_type="smtp_send")
    outcome = normalize_job_outcome(job, {"status": "unexpected_success_like_value"})

    assert outcome.kind == JobOutcomeKind.FAILED
    assert outcome.error_code == "JOB_OUTCOME_INVALID"


def test_old_executor_cannot_finalize_after_fencing_takeover(monkeypatch) -> None:
    from app.services import job_dispatcher

    claimed = JobRunLog(
        id=31,
        job_name="ai_log_maintenance",
        job_type="ai_log_maintenance",
        status="running",
        locked_by="worker-old",
        fencing_token=4,
        attempt_count=1,
        max_attempts=3,
    )
    taken_over = JobRunLog(
        id=31,
        job_name="ai_log_maintenance",
        job_type="ai_log_maintenance",
        status="running",
        locked_by="worker-new",
        fencing_token=5,
        attempt_count=2,
        max_attempts=3,
    )

    async def completed(_session, _job):
        return JobOutcome(JobOutcomeKind.SUCCESS, {"status": "completed"})

    class Session:
        rolled_back = False

        async def scalar(self, _statement):
            return taken_over

        async def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr(job_dispatcher, "dispatch_job", completed)
    session = Session()
    with pytest.raises(JobOwnershipLost):
        asyncio.run(jobs.execute_claimed_job(session, claimed))
    assert session.rolled_back is True


def test_api_has_no_known_long_worker_handler_calls() -> None:
    api_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (BACKEND_DIR / "app" / "api").rglob("*.py")
    )
    for forbidden in (
        "await run_imap_fetch_locked(",
        "await reparse_email(",
        "await create_sn_sync_batch(",
        "await apply_sn_sync_batch(",
        "await poll_export_batch(",
        "await reconcile_uncertain_submission(",
        "parse_sn_assets_xlsx, content",
        "parse_board_cards_file,",
    ):
        assert forbidden not in api_source


def test_executor_timeout_never_blindly_retries_smtp() -> None:
    job = JobRunLog(
        id=9,
        job_name="smtp_send",
        job_type="smtp_send",
        status="running",
        locked_by="worker-1",
        fencing_token=4,
        attempt_count=1,
        max_attempts=3,
        failed_count=0,
    )

    class Session:
        async def scalar(self, _statement):
            return job

    changed = asyncio.run(
        jobs.mark_interrupted_job(
            Session(),
            job_id=9,
            worker_id="worker-1",
            fencing_token=4,
            error_code="JOB_EXECUTION_TIMEOUT",
        )
    )

    assert changed is True
    assert job.status == "needs_manual_review"
    assert job.error_code == "SMTP_DELIVERY_UNCERTAIN"
    assert job.next_run_at is None


def test_terminal_smtp_idempotency_key_creates_retry_lineage(monkeypatch) -> None:
    previous = JobRunLog(
        id=21,
        job_name="smtp_send",
        job_type="smtp_send",
        status="failed",
        resource_type="email_outbox",
        resource_id=8,
        idempotency_key="smtp_outbox:8",
        priority=0,
        max_attempts=3,
        metadata_json={"outbox_id": 8},
    )
    scalar_results = iter((previous, None, None, None))
    added: list[JobRunLog] = []

    class Session:
        async def scalar(self, _statement):
            return next(scalar_results)

        async def get(self, _model, _identity, **_kwargs):
            return previous

        def add(self, row):
            added.append(row)

        async def flush(self):
            added[-1].id = 22

    async def no_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(jobs, "log_system_event", no_log)
    retry = asyncio.run(
        jobs.enqueue_job_or_retry_terminal(
            Session(),
            job_type="smtp_send",
            resource_type="email_outbox",
            resource_id=8,
            idempotency_key="smtp_outbox:8",
            metadata={"outbox_id": 8},
        )
    )

    assert retry is not previous
    assert retry.status == "queued"
    assert retry.retry_of_job_id == previous.id
    assert retry.idempotency_key == f"retry:{previous.id}"


def test_valid_manual_claim_is_never_rewritten_by_explicit_repair() -> None:
    task = ManualReviewTask(
        id=11,
        task_type="mail_review",
        status="claimed",
        claimed_by_user_id=7,
        claimed_at=utcnow(),
        assigned_user_id=7,
    )
    responses = iter(([7], [], [task]))

    class Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class Session:
        async def execute(self, _statement):
            return Result(next(responses))

    result = asyncio.run(repair_notification_and_task_data(Session(), apply=True))

    assert result["counts"].get("normalized_tasks", 0) == 0
    assert task.status == "claimed"
    assert task.claimed_by_user_id == 7
    assert task.assigned_user_id == 7


def test_updated_at_migration_covers_every_model_table() -> None:
    migration = runpy.run_path(
        str(BACKEND_DIR / "alembic" / "versions" / "e8z3a4b5c6d7_fix_updated_at_on_update.py")
    )
    model_tables = {
        table.name for table in Base.metadata.sorted_tables if "updated_at" in table.c
    }

    assert set(migration["UPDATED_AT_TABLES"]) == model_tables
