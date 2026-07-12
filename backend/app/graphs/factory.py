from __future__ import annotations

from langgraph.graph import StateGraph

from app.graphs.email_repair_graph import build_email_repair_graph


def create_email_repair_graph(checkpointer=None) -> StateGraph:
    builder = build_email_repair_graph()
    if checkpointer:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()
