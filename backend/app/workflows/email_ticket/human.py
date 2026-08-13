from __future__ import annotations

from typing import Any

from langgraph.types import interrupt
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from app.workflows.email_ticket.state import EmailTicketRuntime, EmailTicketState


class HumanReviewResult(BaseModel):
    task_id: int
    action: str
    edited_fields: dict[str, Any] = Field(default_factory=dict)
    reviewer_id: int | None = None
    expected_ticket_version: int | None = None
    next_action: str | None = None


def allowed_human_actions(state: EmailTicketState) -> list[str]:
    """Derive review actions from persisted workflow facts, never from the client."""
    actions = ["reparse"]
    if state.get("ticket_id") is not None:
        actions.extend(["validate", "request_customer_info"])
    reply_id = (
        state.get("rma_result", {}).get("reply_id")
        or state.get("reply_result", {}).get("reply_id")
    )
    if reply_id is not None:
        actions.extend(["approve_send", "reject_send"])
    actions.append("close")
    return actions


async def create_human_task(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    existing_task_id = (
        state.get("adoption_result", {}).get("manual_task_id")
        if not state.get("human_result")
        else None
    )
    if existing_task_id is not None:
        return {
            "manual_task_id": int(existing_task_id),
            "execution_state": "waiting_human_review",
            "route_history": [f"human_task:{existing_task_id}:reused"],
        }
    if runtime.context.create_human_task is None:
        raise RuntimeError("HUMAN_TASK_SERVICE_NOT_CONFIGURED")
    expected_ticket_version = (
        state.get("validation_result", {}).get("report", {}).get("snapshot", {}).get("ticket_version")
        or state.get("ticket_version_snapshot")
    )
    reasons = list(state.get("validation_plan", {}).get("reasons") or [])
    workflow_error = state.get("error") or {}
    if workflow_error.get("code"):
        reasons.append(
            f"{workflow_error.get('stage') or 'workflow'}:{workflow_error['code']}"
        )
    if not reasons:
        for result_name in ("archive_result", "send_result", "rma_result", "sap_result", "validation_result"):
            result = state.get(result_name, {})
            if result.get("error_code"):
                reasons.append(str(result["error_code"]))
            elif result.get("status"):
                reasons.append(f"{result_name.upper()}:{result['status']}")
            if reasons:
                break
    task_id = await runtime.context.create_human_task(
        {
            "execution_id": state.get("execution_id"),
            "email_id": state.get("email_id"),
            "ticket_id": state.get("ticket_id"),
            "reasons": reasons or ["MANUAL_REVIEW_REQUIRED"],
            "reply_id": (
                state.get("rma_result", {}).get("reply_id")
                or state.get("reply_result", {}).get("reply_id")
            ),
            "review_type": (
                "rma_reply" if state.get("rma_result", {}).get("reply_id") else "reply"
            ),
        }
    )
    return {
        "manual_task_id": task_id,
        "execution_state": "waiting_human_review",
        "route_history": [f"human_task:{task_id}"],
    }


def wait_human_review(state: EmailTicketState) -> dict[str, Any]:
    """Interrupt before any human result is applied; node replay has no side effects."""
    expected_ticket_version = (
        state.get("validation_result", {}).get("report", {}).get("snapshot", {}).get("ticket_version")
        or state.get("ticket_version_snapshot")
    )
    allowed_actions = allowed_human_actions(state)
    request = {
        "schema_version": "human-review-v1",
        "execution_id": state.get("execution_id"),
        "email_id": state.get("email_id"),
        "ticket_id": state.get("ticket_id"),
        "task_id": state.get("manual_task_id"),
        "reason": state.get("validation_plan", {}).get("reasons") or ["MANUAL_REVIEW_REQUIRED"],
        "reply_id": state.get("rma_result", {}).get("reply_id") or state.get("reply_result", {}).get("reply_id"),
        "expected_ticket_version": expected_ticket_version,
        "allowed_actions": allowed_actions,
    }
    resumed = interrupt(request)
    result = HumanReviewResult.model_validate(resumed)
    if result.task_id != state.get("manual_task_id"):
        raise ValueError("HUMAN_TASK_ID_MISMATCH")
    if result.action not in allowed_actions:
        raise ValueError("HUMAN_ACTION_NOT_ALLOWED")
    if (
        result.expected_ticket_version is not None
        and expected_ticket_version is not None
        and result.expected_ticket_version != expected_ticket_version
    ):
        raise ValueError("HUMAN_TICKET_VERSION_MISMATCH")
    return {
        "human_result": result.model_dump(),
        "execution_state": "human_review_completed",
        "route_history": [f"human:{result.action}"],
    }


async def apply_human_decision(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.apply_human_decision is None:
        raise RuntimeError("HUMAN_DECISION_SERVICE_NOT_CONFIGURED")
    human_result = state.get("human_result", {})
    result = await runtime.context.apply_human_decision(
        {
            **human_result,
            "ticket_id": state.get("ticket_id"),
        }
    )
    applied_action = str(result.get("action") or human_result.get("action") or "")
    return {
        "error": None,
        "human_result": {**human_result, **result, "action": applied_action},
        "execution_state": "human_decision_applied",
        "route_history": [f"human_decision:{applied_action or 'unknown'}"],
    }
