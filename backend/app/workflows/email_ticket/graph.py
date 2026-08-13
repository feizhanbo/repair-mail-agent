from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.workflows.email_ticket import nodes, routers
from app.workflows.email_ticket.human import create_human_task, wait_human_review
from app.workflows.email_ticket.state import EmailTicketRuntime, EmailTicketState


def build_shadow_email_ticket_graph(*, checkpointer=None, interrupt_on_human: bool = False):
    """Compile the read-only graph that stops before deterministic business writes."""
    builder = StateGraph(EmailTicketState, context_schema=EmailTicketRuntime)
    builder.add_node("load_ingested_email", nodes.load_ingested_email)
    builder.add_node("normalize_content", nodes.normalize_content)
    builder.add_node("collect_attachment_results", nodes.collect_attachment_results)
    builder.add_node("classify_and_extract", nodes.classify_and_extract)
    builder.add_node("resolve_business_context", nodes.resolve_business_context)
    builder.add_node("plan_deterministic_validation", nodes.plan_deterministic_validation)
    builder.add_node("finish_human", nodes.mark_shadow_human)
    builder.add_node("finish_terminal", nodes.mark_shadow_terminal)
    builder.add_node("finish_followup", nodes.mark_shadow_followup)
    builder.add_node("finish_validation", nodes.mark_shadow_validation)
    builder.add_node("finish_error", nodes.mark_shadow_error)
    if interrupt_on_human:
        builder.add_node("create_human_task", create_human_task)
        builder.add_node("wait_human_review", wait_human_review)
        builder.add_node("finish_resumed", nodes.mark_shadow_resumed)

    builder.add_edge(START, "load_ingested_email")
    builder.add_conditional_edges(
        "load_ingested_email",
        routers.route_load_result,
        {"normalize": "normalize_content", "error": "finish_error"},
    )
    builder.add_edge("normalize_content", "collect_attachment_results")
    human_target = "create_human_task" if interrupt_on_human else "finish_human"
    builder.add_conditional_edges(
        "collect_attachment_results",
        routers.route_attachment_result,
        {"classify": "classify_and_extract", "human": human_target},
    )
    builder.add_conditional_edges(
        "classify_and_extract",
        routers.route_intent,
        {"auto": "resolve_business_context", "terminal": "finish_terminal", "human": human_target},
    )
    builder.add_edge("resolve_business_context", "plan_deterministic_validation")
    builder.add_conditional_edges(
        "plan_deterministic_validation",
        routers.route_parse_quality,
        {"validate": "finish_validation", "followup": "finish_followup", "human": human_target},
    )
    if interrupt_on_human:
        builder.add_edge("create_human_task", "wait_human_review")
        builder.add_edge("wait_human_review", "finish_resumed")
        builder.add_edge("finish_resumed", END)
    for node in ("finish_human", "finish_terminal", "finish_followup", "finish_validation", "finish_error"):
        builder.add_edge(node, END)
    return builder.compile(checkpointer=checkpointer)
