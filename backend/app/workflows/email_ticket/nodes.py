from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from app.workflows.email_ticket.state import EmailTicketRuntime, EmailTicketState


async def load_ingested_email(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    try:
        snapshot = await runtime.context.load_email(state["email_id"])
    except Exception as exc:
        return {
            "execution_state": "load_ingested_email_failed",
            "error": {"code": exc.__class__.__name__, "stage": "load_ingested_email", "retryable": True},
            "route_history": ["load:error"],
        }
    return {
        "email_snapshot": snapshot,
        "thread_id": snapshot.get("thread_id"),
        "ticket_id": snapshot.get("ticket_id"),
        "ticket_status_snapshot": snapshot.get("ticket_status"),
        "ticket_version_snapshot": snapshot.get("ticket_version"),
        "execution_state": "email_loaded",
        "route_history": ["load:ok"],
    }


def normalize_content(state: EmailTicketState) -> dict[str, Any]:
    email = state.get("email_snapshot", {})
    latest = str(email.get("latest_reply_segment") or "").strip()
    clean = str(email.get("clean_body") or email.get("text_body") or "").strip()
    return {
        "normalized_content": {
            "subject": str(email.get("subject") or ""),
            "latest_reply": latest or clean,
            "clean_body": clean,
        },
        "execution_state": "content_normalized",
        "route_history": ["normalize:ok"],
    }


def collect_attachment_results(state: EmailTicketState) -> dict[str, Any]:
    attachments = state.get("email_snapshot", {}).get("attachments") or []
    results = [dict(item) for item in attachments if isinstance(item, dict)]
    return {
        "attachment_results": results,
        "execution_state": "attachments_collected",
        "route_history": ["attachments:collected"],
    }


async def classify_and_extract(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    email = state.get("email_snapshot", {})
    if runtime.context.classify_email is None:
        result = dict(email.get("parse_result") or {})
        for key in ("intent_type", "intent_subtype", "handling_level", "classification_confidence"):
            if key not in result and key in email:
                result[key] = email.get(key)
    else:
        result = await runtime.context.classify_email(
            {
                "email_id": state["email_id"],
                "content": state.get("normalized_content", {}),
                "attachments": state.get("attachment_results", []),
            }
        )
    return {
        "ai_result": result,
        "execution_state": "classification_completed",
        "route_history": [f"intent:{result.get('intent_type') or 'unknown'}"],
    }


def resolve_business_context(state: EmailTicketState) -> dict[str, Any]:
    email = state.get("email_snapshot", {})
    context = {
        "has_reply_headers": bool(email.get("in_reply_to") or email.get("references_header")),
        "has_active_ticket": state.get("ticket_id") is not None
        and state.get("ticket_status_snapshot") not in {"closed", "resolved"},
        "closed_ticket_context": state.get("ticket_status_snapshot") in {"closed", "resolved"},
    }
    return {
        "business_context": context,
        "execution_state": "business_context_resolved",
        "route_history": ["context:resolved"],
    }


def plan_deterministic_validation(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    result = state.get("ai_result", {})
    missing = result.get("missing_fields") or {}
    conflicts = result.get("conflict_fields") or {}
    confidence = float(result.get("confidence_score") or result.get("classification_confidence") or 0)
    reasons: list[str] = []
    if conflicts:
        outcome = "human_review"
        reasons.append("CONFLICT_FIELDS")
    elif confidence < runtime.context.auto_apply_min_confidence:
        outcome = "human_review"
        reasons.append("LOW_CONFIDENCE")
    elif missing:
        outcome = "request_customer_info"
        reasons.append("MISSING_FIELDS")
    else:
        outcome = "deterministic_validation_required"
        reasons.extend(["SN_LOOKUP_REQUIRED", "CUSTOMER_MATCH_REQUIRED", "POLICY_VALIDATION_REQUIRED"])
    return {
        "validation_plan": {"outcome": outcome, "reasons": reasons},
        "execution_state": "validation_planned",
        "route_history": [f"quality:{outcome}"],
    }


def mark_shadow_human(state: EmailTicketState) -> dict[str, Any]:
    return _finish(state, "human_review_required")


def mark_shadow_terminal(state: EmailTicketState) -> dict[str, Any]:
    return _finish(state, "terminal_without_ticket_automation")


def mark_shadow_followup(state: EmailTicketState) -> dict[str, Any]:
    return _finish(state, "customer_followup_required")


def mark_shadow_validation(state: EmailTicketState) -> dict[str, Any]:
    return _finish(state, "deterministic_validation_required")


def mark_shadow_error(state: EmailTicketState) -> dict[str, Any]:
    return _finish(state, "workflow_error")


def mark_shadow_resumed(state: EmailTicketState) -> dict[str, Any]:
    action = str(state.get("human_result", {}).get("action") or "unknown")
    return _finish(state, f"human_{action}_requested")


def _finish(state: EmailTicketState, outcome: str) -> dict[str, Any]:
    del state
    return {
        "shadow_outcome": outcome,
        "execution_state": "shadow_completed",
        "route_history": [f"finish:{outcome}"],
    }
