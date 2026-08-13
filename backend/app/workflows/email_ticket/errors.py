from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from langgraph.runtime import Runtime

from app.services.logging_safety import safe_error_code
from app.workflows.email_ticket.state import EmailTicketRuntime, EmailTicketState


RecoverableNode = Callable[
    [EmailTicketState, Runtime[EmailTicketRuntime]],
    Awaitable[dict[str, Any]],
]


def recoverable_boundary(stage: str, node: RecoverableNode) -> RecoverableNode:
    """Map deterministic, operator-correctable failures into workflow state.

    Runtime/infrastructure exceptions intentionally escape this boundary so the
    durable job runner can retry them.  HTTP 4xx responses and local data errors
    are not made safer by blind retries and therefore enter the common HITL path.
    """

    async def wrapped(
        state: EmailTicketState,
        runtime: Runtime[EmailTicketRuntime],
    ) -> dict[str, Any]:
        try:
            result = await node(state, runtime)
        except HTTPException as exc:
            if int(exc.status_code) in {408, 425, 429} or not 400 <= int(exc.status_code) < 500:
                raise
            return _recoverable_error(stage, _http_error_code(exc))
        except (LookupError, ValueError) as exc:
            return _recoverable_error(stage, str(exc) or exc.__class__.__name__)
        result["error"] = None
        return result

    wrapped.__name__ = f"{stage}_with_error_boundary"
    return wrapped


def _http_error_code(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("code") or "HTTP_BUSINESS_ERROR")
    return str(detail or f"HTTP_{exc.status_code}")


def _recoverable_error(stage: str, code: str) -> dict[str, Any]:
    normalized = safe_error_code(code, "WORKFLOW_BUSINESS_ERROR") or "WORKFLOW_BUSINESS_ERROR"
    return {
        "error": {
            "code": normalized,
            "stage": stage,
            "retryable": False,
            "recoverable": True,
        },
        "execution_state": "human_review_required",
        "route_history": [f"error:{stage}:{normalized}"],
    }
