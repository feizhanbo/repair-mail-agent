from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from langgraph.runtime import Runtime

from app.services.logging_safety import safe_error_code
from app.workflows.email_ticket.state import EmailTicketRuntime, EmailTicketState


logger = logging.getLogger(__name__)

ObservableNode = Callable[
    [EmailTicketState, Runtime[EmailTicketRuntime]],
    Awaitable[dict[str, Any]],
]
StateOnlyNode = Callable[[EmailTicketState], dict[str, Any]]


def observe_node(stage: str, node: ObservableNode) -> ObservableNode:
    """Persist best-effort lifecycle events for non-interrupting active nodes."""

    async def wrapped(
        state: EmailTicketState,
        runtime: Runtime[EmailTicketRuntime],
    ) -> dict[str, Any]:
        started = monotonic()
        await _record(runtime, _event(stage, "started", state))
        try:
            result = await node(state, runtime)
        except Exception as exc:
            await _record(
                runtime,
                _event(
                    stage,
                    "failed",
                    state,
                    duration_ms=_duration_ms(started),
                    error_code=safe_error_code(exc.__class__.__name__, "WORKFLOW_NODE_ERROR"),
                ),
            )
            raise

        workflow_error = result.get("error") if isinstance(result, dict) else None
        status = "human_required" if isinstance(workflow_error, dict) else "succeeded"
        error_code = workflow_error.get("code") if isinstance(workflow_error, dict) else None
        await _record(
            runtime,
            _event(
                stage,
                status,
                state,
                result=result,
                duration_ms=_duration_ms(started),
                error_code=error_code,
            ),
        )
        return result

    wrapped.__name__ = f"{stage}_with_observability"
    return wrapped


def observe_state_node(stage: str, node: StateOnlyNode) -> ObservableNode:
    """Adapt a deterministic state-only terminal node with lifecycle events."""

    async def invoke(
        state: EmailTicketState,
        _runtime: Runtime[EmailTicketRuntime],
    ) -> dict[str, Any]:
        return node(state)

    return observe_node(stage, invoke)


async def _record(runtime: Runtime[EmailTicketRuntime], event: dict[str, Any]) -> None:
    recorder = runtime.context.record_node_event
    if recorder is None:
        return
    try:
        await recorder(event)
    except Exception:
        # Telemetry must never turn a successful business operation into a retry.
        logger.warning("Unable to persist LangGraph node event", exc_info=True)


def _event(
    stage: str,
    status: str,
    state: EmailTicketState,
    *,
    result: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    route_delta = result.get("route_history") if isinstance(result, dict) else None
    ticket_id = result.get("ticket_id") if isinstance(result, dict) else None
    return {
        "node": stage,
        "status": status,
        "execution_id": state.get("execution_id"),
        "graph_thread_id": state.get("graph_thread_id"),
        "email_id": state.get("email_id"),
        "ticket_id": ticket_id or state.get("ticket_id"),
        "duration_ms": duration_ms,
        "error_code": error_code,
        "route_delta": list(route_delta or []),
    }


def _duration_ms(started: float) -> int:
    return max(0, round((monotonic() - started) * 1000))
