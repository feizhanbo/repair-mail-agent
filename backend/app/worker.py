from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import threading
import time
from contextlib import suppress
from datetime import timedelta

from sqlalchemy import update

from app.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.runtime_logging import configure_runtime_logging
from app.models import JobRunLog
from app.services.common import utcnow
from app.services.jobs import (
    JOB_TIMEOUT_SECONDS,
    HIGH_PRIORITY_STREAK_LIMIT,
    claim_next_job,
    enqueue_job,
    mark_interrupted_job,
    recover_stale_jobs,
)
from app.services.job_dispatcher import validate_job_handlers
from app.services.jobs import JOB_TYPES
from app.services.runtime_config import load_runtime_config
from app.services.schema_gate import assert_schema_current
from app.services.worker_lease import (
    ActiveWorkerLease,
    WorkerLeaseLost,
    acquire_worker_lease,
    assert_worker_lease,
    release_worker_lease,
    renew_worker_lease,
)
from app.services.worker_fencing import JobOwnershipLost


logger = logging.getLogger(__name__)
INSTANCE_ID = f"airma-worker:{socket.gethostname()}:{os.getpid()}"


class EventLoopWatchdog:
    """A non-async liveness guard that can terminate a frozen PID 1."""

    def __init__(self, *, timeout_seconds: float, exit_func=os._exit):
        self.timeout_seconds = max(10.0, float(timeout_seconds))
        self._exit_func = exit_func
        self._last_pulse = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="airma-event-loop-watchdog",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def pulse(self) -> None:
        self._last_pulse = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        interval = max(1.0, min(5.0, self.timeout_seconds / 4))
        while not self._stop.wait(interval):
            if time.monotonic() - self._last_pulse <= self.timeout_seconds:
                continue
            try:
                logger.critical(
                    "AIRMA event loop stopped making progress; terminating worker",
                    extra={"event": "worker_event_loop_stalled"},
                )
            finally:
                # Liveness takes precedence even if a logging handler fails.
                self._exit_func(70)
            return


async def event_loop_pulse_loop(
    stop_event: asyncio.Event, watchdog: EventLoopWatchdog
) -> None:
    while not stop_event.is_set():
        watchdog.pulse()
        await _wait_or_stop(stop_event, 2)


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, seconds))


async def _enqueue_periodic_jobs() -> None:
    now = utcnow()
    async with AsyncSessionLocal() as session:
        await load_runtime_config(session)
        if settings.IMAP_FETCH_ENABLED:
            interval = max(1, settings.IMAP_POLL_INTERVAL_MINUTES)
            bucket = now.replace(minute=(now.minute // interval) * interval, second=0, microsecond=0)
            await enqueue_job(
                session,
                job_type="imap_fetch",
                resource_type="mailbox",
                resource_id=None,
                idempotency_key=f"imap_fetch:scheduled:{settings.IMAP_USER}:{settings.IMAP_FOLDER}:{bucket.isoformat()}",
                metadata={
                    "folder_name": settings.IMAP_FOLDER,
                    "limit": settings.IMAP_INCREMENTAL_LIMIT,
                    "unseen_only": False,
                    "auto_parse": True,
                },
            )

        maintenance_bucket = now.replace(hour=0, minute=0, second=0, microsecond=0)
        await enqueue_job(
            session,
            job_type="ai_log_maintenance",
            resource_type="system",
            resource_id=None,
            idempotency_key=f"ai_log_maintenance:{maintenance_bucket.date().isoformat()}",
        )

        if (
            settings.RELAY_SN_SYNC_ENABLED
            and settings.RELAY_ADAPTER.strip().lower() == "sqlserver"
            and now.hour >= max(0, min(23, settings.RELAY_SQLSERVER_FULL_SYNC_HOUR))
        ):
            await enqueue_job(
                session,
                job_type="sap_sn_sync",
                resource_type="sn_master",
                resource_id=None,
                idempotency_key=f"sap_sn_sync:{now.date().isoformat()}",
                max_attempts=3,
            )

        if settings.RELAY_SQLSERVER_ENABLED:
            poll_interval = max(60, settings.RELAY_SQLSERVER_RMA_POLL_INTERVAL_SECONDS)
            seconds_since_midnight = now.hour * 3600 + now.minute * 60 + now.second
            poll_bucket = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                seconds=(seconds_since_midnight // poll_interval) * poll_interval
            )
            await enqueue_job(
                session,
                job_type="sap_rma_poll",
                resource_type="sap_rma_queue",
                resource_id=None,
                idempotency_key=f"sap_rma_poll:scheduled:{poll_bucket.isoformat()}",
                max_attempts=1,
            )
        await session.commit()


async def scheduler_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await _enqueue_periodic_jobs()
        except Exception:
            logger.exception("Worker scheduler tick failed", extra={"event": "worker_scheduler_failed"})
        await _wait_or_stop(stop_event, 60)


async def _job_heartbeat_loop(
    stop_event: asyncio.Event,
    *,
    job_id: int,
    lease: ActiveWorkerLease,
) -> None:
    while not stop_event.is_set():
        await _wait_or_stop(stop_event, settings.AIRMA_JOB_HEARTBEAT_SECONDS)
        if stop_event.is_set():
            return
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                update(JobRunLog)
                .where(
                    JobRunLog.id == job_id,
                    JobRunLog.status == "running",
                    JobRunLog.locked_by == lease.instance_id,
                    JobRunLog.fencing_token == lease.fencing_token,
                )
                .values(locked_at=utcnow())
            )
            await session.commit()
            if result.rowcount == 0:
                raise JobOwnershipLost("JOB_HEARTBEAT_OWNERSHIP_LOST")


async def _terminate_executor(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def _execute_one(job_id: int, lease: ActiveWorkerLease) -> None:
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _job_heartbeat_loop(heartbeat_stop, job_id=job_id, lease=lease),
        name=f"job-heartbeat-{job_id}",
    )
    try:
        async with AsyncSessionLocal() as session:
            await assert_worker_lease(session, lease)
            job = await session.get(JobRunLog, job_id)
            if job is None:
                return
            timeout_seconds = JOB_TIMEOUT_SECONDS.get(
                job.job_type, settings.ASYNC_JOB_STALE_SECONDS
            )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.worker_executor",
            "--job-id",
            str(job_id),
            "--lease-id",
            str(lease.lease_id),
            "--environment",
            lease.environment,
            "--queue",
            lease.queue_name,
            "--instance-id",
            lease.instance_id,
            "--fencing-token",
            str(lease.fencing_token),
        )
        error_code: str | None = None
        cancelled = False
        process_wait = asyncio.create_task(process.wait(), name=f"executor-wait-{job_id}")
        try:
            done, _ = await asyncio.wait(
                {process_wait, heartbeat},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                error_code = "JOB_EXECUTION_TIMEOUT"
                await _terminate_executor(process)
            elif heartbeat in done:
                await _terminate_executor(process)
                heartbeat.result()
                raise RuntimeError("JOB_HEARTBEAT_STOPPED_UNEXPECTEDLY")
            else:
                return_code = process_wait.result()
                if return_code != 0:
                    error_code = f"JOB_EXECUTOR_EXIT_{return_code}"
        except asyncio.CancelledError:
            cancelled = True
            error_code = "JOB_EXECUTOR_SHUTDOWN"
            await _terminate_executor(process)
        finally:
            if not process_wait.done():
                process_wait.cancel()
                with suppress(asyncio.CancelledError):
                    await process_wait
        if error_code is not None:
            async with AsyncSessionLocal() as session:
                await assert_worker_lease(session, lease)
                changed = await mark_interrupted_job(
                    session,
                    job_id=job_id,
                    worker_id=lease.instance_id,
                    fencing_token=lease.fencing_token,
                    error_code=error_code,
                )
                await session.commit()
            if changed:
                logger.error(
                    "AIRMA job executor was interrupted",
                    extra={"event": "job_executor_interrupted", "job_run_id": job_id, "error_code": error_code},
                )
        if cancelled:
            raise asyncio.CancelledError
    finally:
        heartbeat_stop.set()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


async def consumer_loop(stop_event: asyncio.Event, lease: ActiveWorkerLease) -> None:
    high_priority_streak = 0
    while not stop_event.is_set():
        job_id: int | None = None
        async with AsyncSessionLocal() as session:
            await assert_worker_lease(session, lease)
            job = await claim_next_job(
                session,
                worker_id=lease.instance_id,
                fencing_token=lease.fencing_token,
                prefer_aged_low_priority=high_priority_streak >= HIGH_PRIORITY_STREAK_LIMIT,
            )
            if job is not None:
                job_id = int(job.id)
                high_priority_streak = high_priority_streak + 1 if job.priority <= 1 else 0
            await session.commit()
        if job_id is None:
            await _wait_or_stop(stop_event, settings.ASYNC_JOB_POLL_SECONDS)
            continue
        await _execute_one(job_id, lease)


async def watchdog_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as session:
                count = await recover_stale_jobs(session)
                await session.commit()
            if count:
                logger.warning("Recovered stale jobs", extra={"event": "stale_jobs_recovered", "count": count})
        except Exception:
            logger.exception("Worker watchdog failed", extra={"event": "worker_watchdog_failed"})
        await _wait_or_stop(stop_event, settings.AIRMA_WATCHDOG_INTERVAL_SECONDS)


async def worker_heartbeat_loop(stop_event: asyncio.Event, lease: ActiveWorkerLease) -> None:
    while not stop_event.is_set():
        await _wait_or_stop(stop_event, settings.AIRMA_WORKER_HEARTBEAT_SECONDS)
        if stop_event.is_set():
            return
        async with AsyncSessionLocal() as session:
            await renew_worker_lease(
                session,
                lease,
                lease_seconds=settings.AIRMA_WORKER_LEASE_SECONDS,
            )
            await session.commit()


async def worker_main() -> None:
    configure_runtime_logging()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    lease: ActiveWorkerLease | None = None
    tasks: list[asyncio.Task[None]] = []
    event_loop_watchdog = EventLoopWatchdog(
        timeout_seconds=settings.AIRMA_EVENT_LOOP_WATCHDOG_SECONDS
    )
    try:
        # Start before schema/config/lease I/O so a startup query that wedges
        # the event loop cannot leave an unhealthy container alive forever.
        event_loop_watchdog.start()
        validate_job_handlers(JOB_TYPES)
        async with AsyncSessionLocal() as session:
            await assert_schema_current(session)
            await load_runtime_config(session)
            lease = await acquire_worker_lease(
                session,
                environment=settings.AIRMA_WORKER_ENVIRONMENT,
                queue_name=settings.AIRMA_WORKER_QUEUE,
                instance_id=INSTANCE_ID,
                app_version=f"{settings.APP_VERSION}:{settings.COMMIT_SHA}",
                lease_seconds=settings.AIRMA_WORKER_LEASE_SECONDS,
            )
            await session.commit()
        tasks = [
            asyncio.create_task(
                event_loop_pulse_loop(stop_event, event_loop_watchdog),
                name="event-loop-pulse",
            ),
            asyncio.create_task(scheduler_loop(stop_event), name="scheduler"),
            asyncio.create_task(consumer_loop(stop_event, lease), name="consumer"),
            asyncio.create_task(watchdog_loop(stop_event), name="watchdog"),
            asyncio.create_task(worker_heartbeat_loop(stop_event, lease), name="worker-heartbeat"),
        ]
        logger.info("AIRMA worker started", extra={"event": "airma_worker_started", "worker_instance": INSTANCE_ID})
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            error = task.exception()
            if error is not None:
                raise error
        if stop_event.is_set():
            consumer = next(task for task in tasks if task.get_name() == "consumer")
            if not consumer.done():
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(consumer),
                        timeout=max(1, settings.AIRMA_SHUTDOWN_GRACE_SECONDS),
                    )
        else:
            stop_event.set()
    except WorkerLeaseLost:
        logger.critical("AIRMA worker lost its lease", exc_info=True, extra={"event": "worker_lease_lost"})
        raise
    finally:
        stop_event.set()
        event_loop_watchdog.stop()
        for task in tasks:
            task.cancel()
        if tasks:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(1, settings.AIRMA_SHUTDOWN_GRACE_SECONDS),
                )
        if lease is not None:
            with suppress(Exception):
                async with AsyncSessionLocal() as session:
                    await release_worker_lease(session, lease)
                    await session.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(worker_main())
