from __future__ import annotations

from app.core.email_classification import HandlingLevel
from app.workflows.email_ticket.state import EmailTicketState


def route_load_result(state: EmailTicketState) -> str:
    return "error" if state.get("error") else "normalize"


def route_attachment_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    blocking_statuses = {"needs_manual_review", "unsupported", "failed"}
    for item in state.get("attachment_results", []):
        if item.get("parse_status") in blocking_statuses or item.get("blocks_ticket_flow") is True:
            return "human"
    return "classify"


def route_intent(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    result = state.get("ai_result", {})
    intent = str(result.get("intent_type") or "unknown")
    level = str(result.get("handling_level") or HandlingLevel.UNKNOWN)
    if intent == "irrelevant":
        return "terminal"
    if level == HandlingLevel.AUTO_REPAIR:
        return "auto"
    if level == HandlingLevel.LIFECYCLE_ONLY:
        return "terminal"
    return "human"


def route_parse_quality(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    plan = state.get("validation_plan", {})
    outcome = str(plan.get("outcome") or "human_review")
    if outcome == "request_customer_info":
        return "followup"
    if outcome == "deterministic_validation_required":
        return "validate"
    return "human"


def route_validation_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    status = str(state.get("validation_result", {}).get("status") or "unknown")
    if status == "ready_for_export" and state.get("validation_result", {}).get("sap_required") is True:
        return "sap"
    if status == "ready_for_export":
        return "complete"
    if status in {"need_customer_info", "missing_fields"}:
        return "followup"
    return "human"


def route_sap_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    status = str(state.get("sap_result", {}).get("status") or "unknown")
    if status in {"waiting_sap_result", "waiting_rma", "rma_received"}:
        return "wait"
    if status == "submit_unknown":
        return "reconcile"
    if status == "pending":
        return "submit" if int(state.get("sap_submit_attempt_count") or 0) < 2 else "human"
    if status in {"manual_review", "submit_failed", "superseded", "timed_out"}:
        return "human"
    return "human"


def route_sap_poll_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    status = str(state.get("sap_result", {}).get("status") or "unknown")
    if status == "rma_received":
        return "rma"
    if status in {"waiting_sap_result", "waiting_rma", "submit_unknown"}:
        return "wait"
    if status == "pending":
        return "submit" if int(state.get("sap_submit_attempt_count") or 0) < 2 else "human"
    return "human"


def route_rma_prepare_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    status = str(state.get("rma_result", {}).get("status") or "unknown")
    if status in {"prepared", "approved_pending_send"}:
        return "send"
    if status in {"sent", "succeeded", "closed"}:
        return "archive"
    return "human"


def route_rma_send_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    status = str(state.get("send_result", {}).get("status") or "unknown")
    return "archive" if status == "sent" else "human"


def route_rma_archive_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    status = str(state.get("archive_result", {}).get("status") or "unknown")
    return "complete" if status in {"closed", "archived", "succeeded"} else "human"


def route_human_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    action = str(state.get("human_result", {}).get("action") or "close")
    if action == "validate" and state.get("ticket_id") is not None:
        return "validate"
    if action == "reparse":
        return "reparse"
    if action == "request_customer_info":
        return "prepare_reply" if state.get("ticket_id") is not None else "human"
    if action == "approve_send":
        if state.get("rma_result", {}).get("reply_id"):
            return "send_rma"
        if state.get("reply_result", {}).get("reply_id"):
            return "send_reply"
        return "human"
    if action == "reject_send":
        return "terminal" if (
            state.get("rma_result", {}).get("reply_id")
            or state.get("reply_result", {}).get("reply_id")
        ) else "human"
    return "terminal" if action == "close" else "human"


def route_reply_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    reply_result = state.get("reply_result", {})
    status = str(reply_result.get("status") or "unknown")
    if status != "prepared":
        return "human"
    send_status = str(reply_result.get("send_status") or "unknown")
    return "send" if send_status == "approved_pending_send" else "human"


def route_reply_send_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    return "complete" if state.get("send_result", {}).get("status") == "sent" else "human"


def route_adoption_result(state: EmailTicketState) -> str:
    if state.get("error"):
        return "human"
    result = state.get("adoption_result", {})
    status = str(result.get("email_parse_status") or "unknown")
    if status == "parsed" and result.get("ticket_id") is not None:
        if result.get("missing_fields"):
            return "followup"
        return "validate"
    if status == "skipped":
        return "terminal"
    return "human"


def route_error_or_next(state: EmailTicketState) -> str:
    return "human" if state.get("error") else "next"
