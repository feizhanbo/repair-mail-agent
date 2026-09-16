from __future__ import annotations

import argparse
import asyncio

from app.core.database import AsyncSessionLocal, engine
from app.core.request_context import bind_request_context, reset_request_context
from app.core.runtime_logging import configure_runtime_logging
from app.models import JobRunLog
from app.services.jobs import execute_claimed_job
from app.services.worker_lease import ActiveWorkerLease, assert_worker_lease
from app.services.worker_fencing import JobFence, bind_job_fence, reset_job_fence


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute exactly one fenced AIRMA job")
    parser.add_argument("--job-id", required=True, type=int)
    parser.add_argument("--lease-id", required=True, type=int)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--fencing-token", required=True, type=int)
    return parser.parse_args()


async def execute_one(args: argparse.Namespace) -> None:
    lease = ActiveWorkerLease(
        lease_id=args.lease_id,
        environment=args.environment,
        queue_name=args.queue,
        instance_id=args.instance_id,
        fencing_token=args.fencing_token,
    )
    try:
        async with AsyncSessionLocal() as session:
            await assert_worker_lease(session, lease)
            job = await session.get(JobRunLog, args.job_id)
            if (
                job is None
                or job.status != "running"
                or job.locked_by != lease.instance_id
                or job.fencing_token != lease.fencing_token
            ):
                raise RuntimeError("JOB_OWNERSHIP_LOST")
            tokens = bind_request_context(
                request_id=None,
                correlation_id=job.correlation_id or f"job-{job.id}",
                client_ip=None,
                user_agent=lease.instance_id,
                job_run_id=job.id,
            )
            fence_token = bind_job_fence(
                JobFence(job.id, lease.instance_id, lease.fencing_token)
            )
            try:
                await execute_claimed_job(session, job)
                await assert_worker_lease(session, lease)
                await session.commit()
            finally:
                reset_job_fence(fence_token)
                reset_request_context(tokens)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    configure_runtime_logging()
    asyncio.run(execute_one(_arguments()))
