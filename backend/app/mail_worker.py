from __future__ import annotations

import asyncio
import logging
import socket

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.core.database import AsyncSessionLocal
from app.core.request_context import bind_request_context, reset_request_context
from app.core.runtime_logging import configure_runtime_logging
from app.models import JobRunLog
from app.services.common import utcnow
from app.services.jobs import MAIL_JOB_TYPES, claim_next_job, enqueue_job, execute_claimed_job
from app.services.runtime_config import load_runtime_config


logger = logging.getLogger(__name__)
WORKER_ID = f"mail-worker:{socket.gethostname()}"


async def enqueue_scheduled_imap() -> None:
    async with AsyncSessionLocal() as session:
        await load_runtime_config(session)
        if not settings.MAIL_WORKER_ENABLED or not settings.IMAP_FETCH_ENABLED:
            return
        interval = max(1, settings.IMAP_POLL_INTERVAL_MINUTES)
        now = utcnow()
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
        await session.commit()


async def run_mail_jobs() -> None:
    if not settings.MAIL_WORKER_ENABLED:
        return
    for _ in range(10):
        async with AsyncSessionLocal() as claim_session:
            job = await claim_next_job(
                claim_session,
                worker_id=WORKER_ID,
                job_types=MAIL_JOB_TYPES,
            )
            if job is None:
                await claim_session.commit()
                return
            job_id = int(job.id)
            await claim_session.commit()
        async with AsyncSessionLocal() as run_session:
            claimed_job = await run_session.get(JobRunLog, job_id)
            if claimed_job is None or claimed_job.status != "running":
                continue
            tokens = bind_request_context(
                request_id=None,
                correlation_id=claimed_job.correlation_id or f"job-{claimed_job.id}",
                client_ip=None,
                user_agent=WORKER_ID,
                job_run_id=claimed_job.id,
            )
            try:
                await execute_claimed_job(run_session, claimed_job)
                await run_session.commit()
            finally:
                reset_request_context(tokens)


async def main() -> None:
    configure_runtime_logging()
    async with AsyncSessionLocal() as session:
        await load_runtime_config(session)
        await session.commit()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        enqueue_scheduled_imap,
        "interval",
        minutes=1,
        id="mail_imap_poll",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_mail_jobs,
        "interval",
        seconds=max(1, settings.ASYNC_JOB_POLL_SECONDS),
        id="mail_job_worker",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Independent mail worker started", extra={"event": "mail_worker_started", "worker_instance": WORKER_ID})
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
