from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from langgraph.runtime import Runtime

from app.workflows.email_ticket.observability import observe_node
from app.workflows.email_ticket.state import EmailTicketRuntime


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_observe_node_records_start_and_recoverable_result() -> None:
    events: list[dict] = []

    async def node(_state, _runtime):
        return {
            "error": {"code": "SN_NOT_FOUND", "stage": "validate_ticket"},
            "route_history": ["validation:human"],
            "ticket_id": 12,
        }

    async def record(event: dict) -> None:
        events.append(event)

    runtime = Runtime(
        context=EmailTicketRuntime(load_email=AsyncMock(), record_node_event=record)
    )
    result = await observe_node("validate_ticket", node)(
        {"execution_id": "exec-1", "graph_thread_id": "thread-1", "email_id": 3},
        runtime,
    )

    assert result["error"]["code"] == "SN_NOT_FOUND"
    assert [event["status"] for event in events] == ["started", "human_required"]
    assert events[-1]["route_delta"] == ["validation:human"]
    assert events[-1]["ticket_id"] == 12
    assert isinstance(events[-1]["duration_ms"], int)


@pytest.mark.anyio
async def test_observability_failure_does_not_fail_business_node() -> None:
    async def node(_state, _runtime):
        return {"execution_state": "completed"}

    async def broken_recorder(_event: dict) -> None:
        raise RuntimeError("telemetry unavailable")

    runtime = Runtime(
        context=EmailTicketRuntime(load_email=AsyncMock(), record_node_event=broken_recorder)
    )
    result = await observe_node("finish_completed", node)(
        {"execution_id": "exec-2", "email_id": 4},
        runtime,
    )

    assert result == {"execution_state": "completed"}


@pytest.mark.anyio
async def test_observe_node_records_system_failure_and_reraises() -> None:
    events: list[dict] = []

    async def node(_state, _runtime):
        raise RuntimeError("database unavailable")

    async def record(event: dict) -> None:
        events.append(event)

    runtime = Runtime(
        context=EmailTicketRuntime(load_email=AsyncMock(), record_node_event=record)
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await observe_node("submit_sap", node)(
            {"execution_id": "exec-3", "email_id": 5},
            runtime,
        )

    assert [event["status"] for event in events] == ["started", "failed"]
    assert events[-1]["error_code"] == "RUNTIMEERROR"
