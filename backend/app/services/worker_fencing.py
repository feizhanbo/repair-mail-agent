from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JobRunLog


class JobOwnershipLost(RuntimeError):
    pass


@dataclass(frozen=True)
class JobFence:
    job_id: int
    worker_id: str
    fencing_token: int


_current_job_fence: ContextVar[JobFence | None] = ContextVar(
    "airma_current_job_fence", default=None
)


def bind_job_fence(fence: JobFence) -> Token[JobFence | None]:
    return _current_job_fence.set(fence)


def reset_job_fence(token: Token[JobFence | None]) -> None:
    _current_job_fence.reset(token)


async def assert_job_fence(session: AsyncSession, fence: JobFence | None = None) -> None:
    expected = fence or _current_job_fence.get()
    if expected is None:
        return
    owner = await session.scalar(
        select(JobRunLog.id)
        .where(
            JobRunLog.id == expected.job_id,
            JobRunLog.status == "running",
            JobRunLog.locked_by == expected.worker_id,
            JobRunLog.fencing_token == expected.fencing_token,
        )
        .with_for_update()
    )
    if owner is None:
        raise JobOwnershipLost("JOB_OWNERSHIP_LOST")


async def guarded_commit(session: AsyncSession) -> None:
    """Commit a durable checkpoint only while this Executor still owns the job."""
    await assert_job_fence(session)
    await session.commit()
