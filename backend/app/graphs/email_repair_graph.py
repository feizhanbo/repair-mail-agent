from __future__ import annotations

from langgraph.graph import StateGraph, END

from app.graphs.state import EmailRepairState
from app.graphs.constants import (
    NODE_LOAD_EMAIL,
    NODE_RULE_EXTRACT,
    NODE_AI_FULL_PARSE,
    NODE_APPLY_TICKET,
    NODE_FINALIZE,
    NODE_ERROR_ESCALATE,
)
from app.graphs.nodes.parse_nodes import load_email_node, rule_extract_node
from app.graphs.nodes.ai_nodes import ai_full_parse_node
from app.graphs.nodes.ticket_nodes import apply_ticket_service_node, finalize_node
from app.graphs.edges.conditions import should_skip_ai, after_ai_route, after_ticket_route


def build_email_repair_graph() -> StateGraph:
    builder = StateGraph(EmailRepairState)

    builder.add_node(NODE_LOAD_EMAIL, load_email_node)
    builder.add_node(NODE_RULE_EXTRACT, rule_extract_node)
    builder.add_node(NODE_AI_FULL_PARSE, ai_full_parse_node)
    builder.add_node(NODE_APPLY_TICKET, apply_ticket_service_node)
    builder.add_node(NODE_FINALIZE, finalize_node)

    builder.set_entry_point(NODE_LOAD_EMAIL)

    builder.add_edge(NODE_LOAD_EMAIL, NODE_RULE_EXTRACT)

    builder.add_conditional_edges(
        NODE_RULE_EXTRACT,
        should_skip_ai,
        {
            NODE_AI_FULL_PARSE: NODE_AI_FULL_PARSE,
            NODE_APPLY_TICKET: NODE_APPLY_TICKET,
        },
    )

    builder.add_conditional_edges(
        NODE_AI_FULL_PARSE,
        after_ai_route,
        {
            NODE_APPLY_TICKET: NODE_APPLY_TICKET,
            NODE_ERROR_ESCALATE: NODE_ERROR_ESCALATE,
        },
    )

    builder.add_conditional_edges(
        NODE_APPLY_TICKET,
        after_ticket_route,
        {
            NODE_FINALIZE: NODE_FINALIZE,
            NODE_ERROR_ESCALATE: NODE_ERROR_ESCALATE,
        },
    )

    builder.add_edge(NODE_FINALIZE, END)
    builder.add_edge(NODE_ERROR_ESCALATE, END)

    return builder
