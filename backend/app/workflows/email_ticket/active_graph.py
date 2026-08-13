from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.workflows.email_ticket import external, routers
from app.workflows.email_ticket.errors import recoverable_boundary
from app.workflows.email_ticket.human import apply_human_decision, create_human_task, wait_human_review
from app.workflows.email_ticket.observability import observe_node, observe_state_node
from app.workflows.email_ticket.state import EmailTicketRuntime, EmailTicketState


def build_active_email_ticket_graph(*, checkpointer):
    """Authoritative active graph from persisted email parsing through delivery."""
    builder = StateGraph(EmailTicketState, context_schema=EmailTicketRuntime)
    builder.add_node("prepare_email_parse", _active_node("prepare_email_parse", external.prepare_email_parse))
    builder.add_node("generate_ai_candidate", _active_node("generate_ai_candidate", external.generate_ai_candidate))
    builder.add_node("adopt_email_candidate", _active_node("adopt_email_candidate", external.adopt_email_candidate))
    builder.add_node("validate_ticket", _active_node("validate_ticket", external.validate_ticket))
    builder.add_node("submit_sap", _active_node("submit_sap", external.submit_sap))
    builder.add_node("reconcile_sap", _active_node("reconcile_sap", external.reconcile_sap))
    builder.add_node("wait_external_result", external.wait_external_result)
    builder.add_node("poll_sap", _active_node("poll_sap", external.poll_sap))
    builder.add_node("prepare_rma", _active_node("prepare_rma", external.prepare_rma))
    builder.add_node("send_rma", _active_node("send_rma", external.send_rma))
    builder.add_node("finalize_rma_archive", _active_node("finalize_rma_archive", external.finalize_rma_archive))
    builder.add_node("prepare_reply", _active_node("prepare_reply", external.prepare_reply))
    builder.add_node("send_reply", _active_node("send_reply", external.send_reply))
    builder.add_node("create_human_task", observe_node("create_human_task", create_human_task))
    builder.add_node("wait_human_review", wait_human_review)
    builder.add_node("apply_human_decision", _active_node("apply_human_decision", apply_human_decision))
    builder.add_node("finish_resumed", observe_state_node("finish_resumed", external.finish_human_resolution))
    builder.add_node("finish_terminal", observe_state_node("finish_terminal", external.finish_terminal))
    builder.add_node("finish_followup", observe_state_node("finish_followup", external.finish_reply_delivery))
    builder.add_node("finish_validation", observe_state_node("finish_validation", external.finish_reply_delivery))
    builder.add_node("finish_completed", observe_state_node("finish_completed", external.finish_completed))

    builder.add_edge(START, "prepare_email_parse")
    builder.add_conditional_edges("prepare_email_parse", routers.route_error_or_next, {"next": "generate_ai_candidate", "human": "create_human_task"})
    builder.add_conditional_edges("generate_ai_candidate", routers.route_error_or_next, {"next": "adopt_email_candidate", "human": "create_human_task"})
    builder.add_conditional_edges(
        "adopt_email_candidate",
        routers.route_adoption_result,
        {"validate": "validate_ticket", "followup": "prepare_reply", "terminal": "finish_terminal", "human": "create_human_task"},
    )
    builder.add_conditional_edges("validate_ticket", routers.route_validation_result, {"sap": "submit_sap", "complete": "prepare_reply", "followup": "prepare_reply", "human": "create_human_task"})
    builder.add_conditional_edges("submit_sap", routers.route_sap_result, {"wait": "wait_external_result", "reconcile": "reconcile_sap", "submit": "submit_sap", "human": "create_human_task"})
    builder.add_conditional_edges("reconcile_sap", routers.route_sap_result, {"wait": "wait_external_result", "reconcile": "wait_external_result", "submit": "submit_sap", "human": "create_human_task"})
    builder.add_edge("wait_external_result", "poll_sap")
    builder.add_conditional_edges("poll_sap", routers.route_sap_poll_result, {"wait": "wait_external_result", "rma": "prepare_rma", "submit": "submit_sap", "human": "create_human_task"})
    builder.add_conditional_edges("prepare_rma", routers.route_rma_prepare_result, {"send": "send_rma", "archive": "finalize_rma_archive", "human": "create_human_task"})
    builder.add_conditional_edges("send_rma", routers.route_rma_send_result, {"archive": "finalize_rma_archive", "human": "create_human_task"})
    builder.add_conditional_edges("finalize_rma_archive", routers.route_rma_archive_result, {"complete": "finish_completed", "human": "create_human_task"})
    builder.add_conditional_edges("prepare_reply", routers.route_reply_result, {"send": "send_reply", "human": "create_human_task"})
    builder.add_conditional_edges("send_reply", routers.route_reply_send_result, {"complete": "finish_validation", "human": "create_human_task"})
    builder.add_edge("create_human_task", "wait_human_review")
    builder.add_edge("wait_human_review", "apply_human_decision")
    builder.add_conditional_edges(
        "apply_human_decision",
        routers.route_human_result,
        {"human": "create_human_task", "validate": "validate_ticket", "reparse": "prepare_email_parse", "prepare_reply": "prepare_reply", "send_rma": "send_rma", "send_reply": "send_reply", "terminal": "finish_resumed"},
    )
    for finish in ("finish_resumed", "finish_terminal", "finish_followup", "finish_validation", "finish_completed"):
        builder.add_edge(finish, END)
    return builder.compile(checkpointer=checkpointer)


def _active_node(stage: str, node):
    return observe_node(stage, recoverable_boundary(stage, node))
