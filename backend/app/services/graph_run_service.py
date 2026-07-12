from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.graph_repository import get_graph_run, get_node_logs_for_run

logger = logging.getLogger(__name__)

_runner = None


def get_graph_runner():
    global _runner
    if _runner is None:
        from app.graphs.runner import GraphRunner
        _runner = GraphRunner()
    return _runner


def init_graph_runner(checkpointer=None):
    global _runner
    from app.graphs.runner import GraphRunner
    _runner = GraphRunner(checkpointer=checkpointer)
    return _runner


async def start_email_repair_graph(
    session: AsyncSession,
    *,
    email_id: int,
    user_id: int | None = None,
    reason: str = "api_trigger",
) -> dict[str, Any]:
    runner = get_graph_runner()
    return await runner.invoke(session, email_id=email_id, user_id=user_id, reason=reason)


async def get_graph_run_status(session: AsyncSession, graph_run_id: str) -> dict[str, Any] | None:
    run = await get_graph_run(session, graph_run_id)
    if run is None:
        return None
    node_logs = await get_node_logs_for_run(session, run.id)
    return {
        "graph_run_id": run.graph_run_id,
        "status": run.status,
        "graph_name": run.graph_name,
        "current_node": run.current_node,
        "email_id": run.email_id,
        "ticket_id": run.ticket_id,
        "trigger_source": run.trigger_source,
        "interrupt_type": run.interrupt_type,
        "resume_count": run.resume_count,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_ms": run.duration_ms,
        "error_message": run.error_message,
        "metadata": run.metadata_json,
        "created_at": run.created_at,
        "node_logs": [
            {
                "id": nl.id,
                "node_name": nl.node_name,
                "node_type": nl.node_type,
                "status": nl.status,
                "retry_count": nl.retry_count,
                "duration_ms": nl.duration_ms,
                "error_code": nl.error_code,
                "error_message": nl.error_message,
                "started_at": nl.started_at,
                "finished_at": nl.finished_at,
            }
            for nl in node_logs
        ],
    }
