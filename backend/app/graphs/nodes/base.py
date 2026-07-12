from __future__ import annotations

import time
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graphs import GraphRun, GraphNodeLog

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def log_node_start(
    session: AsyncSession,
    graph_run_id: int,
    node_name: str,
    node_type: str = "service",
    input_summary: str | None = None,
) -> GraphNodeLog:
    node_log = GraphNodeLog(
        graph_run_id=graph_run_id,
        node_name=node_name,
        node_type=node_type,
        status="running",
        started_at=utcnow(),
        input_summary=input_summary[:500] if input_summary else None,
    )
    session.add(node_log)
    await session.flush()
    return node_log


async def log_node_end(
    session: AsyncSession,
    node_log: GraphNodeLog,
    status: str = "completed",
    output_summary: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    node_log.status = status
    node_log.finished_at = utcnow()
    if node_log.started_at:
        node_log.duration_ms = int((utcnow() - node_log.started_at).total_seconds() * 1000)
    if output_summary:
        node_log.output_summary = output_summary[:500]
    if error_code:
        node_log.error_code = error_code
    if error_message:
        node_log.error_message = error_message


async def node_wrapper(
    session: AsyncSession,
    state: dict[str, Any],
    node_name: str,
    node_func: callable,
    node_type: str = "service",
    input_summary: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    state["current_node"] = node_name
    node_log = await log_node_start(
        session,
        graph_run_id=state.get("graph_run_db_id", 0),
        node_name=node_name,
        node_type=node_type,
        input_summary=input_summary,
    )
    try:
        result = await node_func(session, state)
        await log_node_end(session, node_log, status="completed", output_summary=str(result)[:500])
        if isinstance(result, dict):
            state.update(result)
        return state
    except Exception as exc:
        await log_node_end(
            session,
            node_log,
            status="failed",
            error_code=exc.__class__.__name__,
            error_message=str(exc)[:500],
        )
        state["error_message"] = f"[{node_name}] {exc}"
        raise
