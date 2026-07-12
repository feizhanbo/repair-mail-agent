from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class GraphRunStartRequest(BaseModel):
    email_id: int
    reason: str = "api_trigger"


class GraphRunResponse(BaseModel):
    graph_run_id: str
    status: str
    graph_name: str | None = None
    current_node: str | None = None
    email_id: int | None = None
    ticket_id: int | None = None
    trigger_source: str | None = None
    interrupt_type: str | None = None
    resume_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error_message: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None

    class Config:
        from_attributes = True


class GraphNodeLogResponse(BaseModel):
    id: int
    node_name: str
    node_type: str
    status: str
    retry_count: int = 0
    duration_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    class Config:
        from_attributes = True


class GraphRunDetailResponse(GraphRunResponse):
    node_logs: list[GraphNodeLogResponse] = []
