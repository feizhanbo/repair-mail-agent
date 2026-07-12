from __future__ import annotations

import uuid
import time
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.graphs import GraphRun
from app.graphs.factory import create_email_repair_graph

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class GraphRunner:
    def __init__(self, checkpointer=None):
        self._checkpointer = checkpointer
        self._compiled = None

    @property
    def compiled(self):
        if self._compiled is None:
            self._compiled = create_email_repair_graph(checkpointer=self._checkpointer)
        return self._compiled

    async def invoke(
        self,
        session: AsyncSession,
        *,
        email_id: int,
        user_id: int | None = None,
        reason: str = "api_trigger",
        trigger_source: str = "api",
    ) -> dict[str, Any]:
        graph_run_id = str(uuid.uuid4())
        run = GraphRun(
            graph_run_id=graph_run_id,
            graph_name="email_repair",
            email_id=email_id,
            trigger_source=trigger_source,
            status="running",
            started_at=_utcnow(),
            metadata_json={"email_id": email_id, "reason": reason, "user_id": user_id},
        )
        session.add(run)
        await session.flush()
        await session.refresh(run)

        state: dict[str, Any] = {
            "email_id": email_id,
            "user_id": user_id,
            "reason": reason,
            "graph_run_id": graph_run_id,
            "graph_run_db_id": run.id,
            "skip_ai": False,
            "requires_manual": False,
            "should_create_reply": False,
        }

        started = time.monotonic()
        try:
            if not settings.LANGGRAPH_ENABLED:
                run.status = "skipped"
                run.finished_at = _utcnow()
                run.error_message = "LangGraph disabled by config"
                return {"status": "skipped", "message": "LangGraph disabled by config"}

            final_state = await self.compiled.ainvoke(state)
            run.status = "completed"
            run.finished_at = _utcnow()
            run.duration_ms = int((time.monotonic() - started) * 1000)
            if run.metadata_json:
                run.metadata_json = {**run.metadata_json, "final_state_summary": final_state.get("summary")}
            else:
                run.metadata_json = {"final_state_summary": final_state.get("summary")}
            return {"status": "completed", "graph_run_id": graph_run_id, "result": final_state.get("summary")}

        except Exception as exc:
            run.status = "failed"
            run.finished_at = _utcnow()
            run.duration_ms = int((time.monotonic() - started) * 1000)
            run.error_message = f"{exc.__class__.__name__}: {exc}"
            logger.exception("GraphRunner.invoke failed for email_id=%s", email_id)
            return {"status": "failed", "graph_run_id": graph_run_id, "error": str(exc)}
