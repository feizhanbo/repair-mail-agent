from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graphs import GraphRun, GraphNodeLog
from app.services.common import paginate_scalars


async def get_graph_run(session: AsyncSession, graph_run_id: str) -> GraphRun | None:
    return await session.scalar(select(GraphRun).where(GraphRun.graph_run_id == graph_run_id))


async def get_graph_run_by_id(session: AsyncSession, run_id: int) -> GraphRun | None:
    return await session.get(GraphRun, run_id)


async def list_graph_runs(session: AsyncSession, *, page: int = 1, page_size: int = 20, status: str | None = None) -> tuple[list[GraphRun], int]:
    statement = select(GraphRun).order_by(GraphRun.created_at.desc())
    if status:
        statement = statement.where(GraphRun.status == status)
    return await paginate_scalars(session, statement, page, page_size)


async def get_node_logs_for_run(session: AsyncSession, graph_run_id: int) -> list[GraphNodeLog]:
    result = await session.execute(select(GraphNodeLog).where(GraphNodeLog.graph_run_id == graph_run_id).order_by(GraphNodeLog.started_at.asc()))
    return list(result.scalars().all())
