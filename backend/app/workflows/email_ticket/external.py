from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.workflows.email_ticket.state import EmailTicketRuntime, EmailTicketState


async def prepare_email_parse(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.prepare_email_parse is None:
        raise RuntimeError("EMAIL_PARSE_SERVICE_NOT_CONFIGURED")
    result = await runtime.context.prepare_email_parse(
        {
            "email_id": state["email_id"],
            "execution_id": state.get("execution_id"),
            **state.get("parse_request", {}),
        }
    )
    return {
        "parse_context": result,
        "execution_state": "email_parse_prepared",
        "route_history": ["parse_prepare:ok"],
    }


async def generate_ai_candidate(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.generate_ai_candidate is None:
        raise RuntimeError("AI_CANDIDATE_SERVICE_NOT_CONFIGURED")
    result = await runtime.context.generate_ai_candidate(state["parse_context"])
    return {
        "ai_candidate": result,
        "execution_state": "ai_candidate_generated",
        "route_history": [f"ai_candidate:{'available' if result.get('ai_available') else 'unavailable'}"],
    }


async def adopt_email_candidate(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.adopt_email_candidate is None:
        raise RuntimeError("EMAIL_ADOPTION_SERVICE_NOT_CONFIGURED")
    result = await runtime.context.adopt_email_candidate(
        {"parse_context": state["parse_context"], "ai_candidate": state.get("ai_candidate", {})}
    )
    return {
        "adoption_result": result,
        "ticket_id": result.get("ticket_id"),
        "ticket_version_snapshot": result.get("ticket_version"),
        "ai_result": {
            "intent_type": result.get("intent_type"),
            "intent_subtype": result.get("intent_subtype"),
            "handling_level": result.get("handling_level"),
            "confidence_score": result.get("confidence_score"),
            "missing_fields": result.get("missing_fields") or {},
            "conflict_fields": result.get("conflict_fields") or {},
        },
        "execution_state": "email_candidate_adopted",
        "route_history": [f"adoption:{result.get('email_parse_status') or 'unknown'}"],
    }


async def validate_ticket(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.validate_ticket is None or state.get("ticket_id") is None:
        raise RuntimeError("VALIDATION_SERVICE_NOT_CONFIGURED")
    human_result = state.get("human_result") or {}
    resolving_task_id = (
        human_result.get("task_id")
        if str(human_result.get("action") or "") == "validate"
        else None
    )
    result = await runtime.context.validate_ticket(
        {
            "ticket_id": state["ticket_id"],
            "resolving_task_id": resolving_task_id,
        }
    )
    return {
        "validation_result": result,
        "execution_state": "ticket_validated",
        "route_history": [f"validation:{result.get('status') or 'unknown'}"],
    }


async def submit_sap(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.submit_sap is None:
        raise RuntimeError("SAP_SUBMIT_SERVICE_NOT_CONFIGURED")
    export_id = state.get("validation_result", {}).get("export_id")
    if export_id is None:
        raise ValueError("SAP_EXPORT_ID_MISSING")
    result = await runtime.context.submit_sap(int(export_id))
    previous_export_id = state.get("sap_submit_export_id")
    attempt_count = (
        int(state.get("sap_submit_attempt_count") or 0) + 1
        if previous_export_id == int(export_id)
        else 1
    )
    return {
        "sap_result": result,
        "sap_submit_attempt_count": attempt_count,
        "sap_submit_export_id": int(export_id),
        "execution_state": "sap_submit_completed",
        "route_history": [f"sap_submit:{result.get('status') or 'unknown'}"],
    }


async def reconcile_sap(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.reconcile_sap is None:
        raise RuntimeError("SAP_RECONCILE_SERVICE_NOT_CONFIGURED")
    export_id = state.get("sap_result", {}).get("export_id")
    if export_id is None:
        raise ValueError("SAP_EXPORT_ID_MISSING")
    result = await runtime.context.reconcile_sap(int(export_id))
    return {
        "sap_result": result,
        "execution_state": "sap_reconciliation_completed",
        "route_history": [f"sap_reconcile:{result.get('status') or 'unknown'}"],
    }


def wait_external_result(state: EmailTicketState) -> dict[str, Any]:
    """Persist a scheduler-safe wait point; resuming this node performs no side effect."""
    result = state.get("sap_result", {})
    export_id = result.get("export_id") or state.get("validation_result", {}).get("export_id")
    resumed = interrupt(
        {
            "schema_version": "external-wait-v1",
            "execution_id": state.get("execution_id"),
            "ticket_id": state.get("ticket_id"),
            "export_id": export_id,
            "status": result.get("status"),
            "next_poll_seconds": result.get("next_poll_seconds"),
        }
    )
    return {
        "execution_state": "external_wait_resumed",
        "route_history": [f"external_resume:{resumed.get('reason', 'scheduled_poll') if isinstance(resumed, dict) else 'scheduled_poll'}"],
    }


async def poll_sap(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.poll_sap is None:
        raise RuntimeError("SAP_POLL_SERVICE_NOT_CONFIGURED")
    export_id = state.get("sap_result", {}).get("export_id") or state.get("validation_result", {}).get("export_id")
    if export_id is None:
        raise ValueError("SAP_EXPORT_ID_MISSING")
    result = await runtime.context.poll_sap(int(export_id))
    return {
        "sap_result": result,
        "execution_state": "sap_poll_completed",
        "route_history": [f"sap_poll:{result.get('status') or 'unknown'}"],
    }


async def prepare_rma(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.prepare_rma is None or state.get("ticket_id") is None:
        raise RuntimeError("RMA_PREPARE_SERVICE_NOT_CONFIGURED")
    result = await runtime.context.prepare_rma(
        {
            "ticket_id": state["ticket_id"],
            "rma_no": state.get("sap_result", {}).get("rma_no"),
        }
    )
    return {
        "rma_result": result,
        "execution_state": "rma_prepare_completed",
        "route_history": [f"rma_prepare:{result.get('status') or 'unknown'}"],
    }


async def send_rma(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.send_rma is None:
        raise RuntimeError("RMA_SEND_SERVICE_NOT_CONFIGURED")
    reply_id = state.get("rma_result", {}).get("reply_id")
    if reply_id is None:
        raise ValueError("RMA_REPLY_ID_MISSING")
    result = await runtime.context.send_rma(int(reply_id))
    return {
        "send_result": result,
        "execution_state": "rma_send_completed",
        "route_history": [f"rma_send:{result.get('status') or 'unknown'}"],
    }


async def finalize_rma_archive(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.finalize_rma_archive is None:
        raise RuntimeError("RMA_ARCHIVE_SERVICE_NOT_CONFIGURED")
    reply_id = state.get("send_result", {}).get("reply_id") or state.get("rma_result", {}).get("reply_id")
    if reply_id is None:
        raise ValueError("RMA_REPLY_ID_MISSING")
    result = await runtime.context.finalize_rma_archive(int(reply_id))
    return {
        "archive_result": result,
        "execution_state": "rma_archive_completed",
        "route_history": [f"rma_archive:{result.get('status') or 'unknown'}"],
    }


async def prepare_reply(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.prepare_reply is None or state.get("ticket_id") is None:
        raise RuntimeError("REPLY_PREPARE_SERVICE_NOT_CONFIGURED")
    missing = state.get("ai_result", {}).get("missing_fields") or {}
    result = await runtime.context.prepare_reply(
        {
            "ticket_id": state["ticket_id"],
            "email_id": state["email_id"],
            "reply_type": "missing_fields" if missing else "receipt",
            "missing_fields": missing,
        }
    )
    return {
        "reply_result": result,
        "execution_state": "reply_prepare_completed",
        "route_history": [f"reply_prepare:{result.get('status') or 'unknown'}"],
    }


async def send_reply(
    state: EmailTicketState,
    runtime: Runtime[EmailTicketRuntime],
) -> dict[str, Any]:
    if runtime.context.send_reply is None:
        raise RuntimeError("REPLY_SEND_SERVICE_NOT_CONFIGURED")
    reply_id = state.get("reply_result", {}).get("reply_id")
    if reply_id is None:
        raise ValueError("REPLY_ID_MISSING")
    result = await runtime.context.send_reply(int(reply_id))
    return {
        "send_result": result,
        "execution_state": "reply_send_completed",
        "route_history": [f"reply_send:{result.get('status') or 'unknown'}"],
    }


def finish_external_wait(state: EmailTicketState) -> dict[str, Any]:
    status = str(state.get("sap_result", {}).get("status") or "waiting")
    return {
        "shadow_outcome": status,
        "execution_state": "waiting_external",
        "route_history": [f"external_wait:{status}"],
    }


def finish_external_manual(state: EmailTicketState) -> dict[str, Any]:
    status = str(state.get("sap_result", {}).get("status") or "manual_review")
    return {
        "shadow_outcome": "external_manual_review",
        "execution_state": "external_manual_review",
        "route_history": [f"external_manual:{status}"],
    }


def finish_completed(state: EmailTicketState) -> dict[str, Any]:
    status = str(state.get("archive_result", {}).get("status") or "completed")
    return {
        "workflow_outcome": "completed",
        # Compatibility for the pre-authoritative validation graph. Runtime
        # persistence prefers workflow_outcome and no longer derives execution
        # semantics from this legacy comparison field.
        "shadow_outcome": "completed",
        "execution_state": "completed",
        "route_history": [f"completed:{status}"],
    }


def finish_terminal(state: EmailTicketState) -> dict[str, Any]:
    del state
    return {
        "workflow_outcome": "terminal_without_ticket_automation",
        "execution_state": "completed",
        "route_history": ["finish:terminal_without_ticket_automation"],
    }


def finish_reply_delivery(state: EmailTicketState) -> dict[str, Any]:
    missing = state.get("ai_result", {}).get("missing_fields") or {}
    outcome = "customer_followup_sent" if missing else "reply_sent"
    return {
        "workflow_outcome": outcome,
        "execution_state": "completed",
        "route_history": [f"finish:{outcome}"],
    }


def finish_human_resolution(state: EmailTicketState) -> dict[str, Any]:
    action = str(state.get("human_result", {}).get("action") or "close")
    return {
        "workflow_outcome": f"human_{action}_completed",
        "execution_state": "completed",
        "route_history": [f"finish:human_{action}_completed"],
    }
