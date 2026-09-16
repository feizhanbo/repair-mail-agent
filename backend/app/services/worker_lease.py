from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WorkerLease
from app.services.common import utcnow


class WorkerLeaseUnavailable(RuntimeError):
    pass


class WorkerLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class ActiveWorkerLease:
    lease_id: int
    environment: str
    queue_name: str
    instance_id: str
    fencing_token: int


async def acquire_worker_lease(
    session: AsyncSession,
    *,
    environment: str,
    queue_name: str,
    instance_id: str,
    app_version: str,
    lease_seconds: int,
) -> ActiveWorkerLease:
    now = utcnow()
    row = await session.scalar(
        select(WorkerLease)
        .where(
            WorkerLease.environment == environment,
            WorkerLease.queue_name == queue_name,
        )
        .with_for_update()
    )
    if row is None:
        row = WorkerLease(
            environment=environment,
            queue_name=queue_name,
            instance_id=instance_id,
            app_version=app_version,
            fencing_token=1,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=max(1, lease_seconds)),
        )
        session.add(row)
        await session.flush()
    elif row.instance_id != instance_id and row.lease_expires_at > now:
        raise WorkerLeaseUnavailable(
            f"WORKER_LEASE_HELD:{row.instance_id}:{row.lease_expires_at.isoformat()}"
        )
    else:
        if row.instance_id != instance_id:
            row.fencing_token += 1
        row.instance_id = instance_id
        row.app_version = app_version
        row.heartbeat_at = now
        row.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
        await session.flush()
    return ActiveWorkerLease(
        lease_id=row.id,
        environment=row.environment,
        queue_name=row.queue_name,
        instance_id=row.instance_id,
        fencing_token=row.fencing_token,
    )


async def renew_worker_lease(
    session: AsyncSession,
    lease: ActiveWorkerLease,
    *,
    lease_seconds: int,
) -> None:
    now = utcnow()
    row = await session.get(WorkerLease, lease.lease_id, with_for_update=True)
    if (
        row is None
        or row.instance_id != lease.instance_id
        or row.fencing_token != lease.fencing_token
        or row.lease_expires_at <= now
    ):
        raise WorkerLeaseLost("WORKER_LEASE_LOST")
    row.heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))


async def assert_worker_lease(session: AsyncSession, lease: ActiveWorkerLease) -> None:
    now = utcnow()
    row = await session.scalar(
        select(WorkerLease)
        .where(WorkerLease.id == lease.lease_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        row is None
        or row.instance_id != lease.instance_id
        or row.fencing_token != lease.fencing_token
        or row.lease_expires_at <= now
    ):
        raise WorkerLeaseLost("WORKER_LEASE_LOST")


async def release_worker_lease(session: AsyncSession, lease: ActiveWorkerLease) -> None:
    row = await session.get(WorkerLease, lease.lease_id, with_for_update=True)
    if row is None or row.instance_id != lease.instance_id or row.fencing_token != lease.fencing_token:
        return
    now = utcnow()
    row.heartbeat_at = now
    row.lease_expires_at = now
