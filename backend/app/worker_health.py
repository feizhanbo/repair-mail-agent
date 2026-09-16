from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.models import WorkerLease
from app.services.common import utcnow


async def check_worker_health() -> bool:
    try:
        async with AsyncSessionLocal() as session:
            lease = await session.scalar(
                select(WorkerLease).where(
                    WorkerLease.environment == settings.AIRMA_WORKER_ENVIRONMENT,
                    WorkerLease.queue_name == settings.AIRMA_WORKER_QUEUE,
                    WorkerLease.lease_expires_at > utcnow(),
                )
            )
            return lease is not None
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(check_worker_health()) else 1)
