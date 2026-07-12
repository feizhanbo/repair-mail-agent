from __future__ import annotations

from typing import Any, Literal

from app.graphs.constants import (
    NODE_AI_FULL_PARSE,
    NODE_APPLY_TICKET,
    NODE_FINALIZE,
    NODE_ERROR_ESCALATE,
    SKIPPABLE_INTENTS,
)


def should_skip_ai(state: dict[str, Any]) -> Literal["ai_full_parse_node", "apply_ticket_service_node"]:
    if state.get("skip_ai", False) or state.get("intent_type") in SKIPPABLE_INTENTS:
        return NODE_APPLY_TICKET
    return NODE_AI_FULL_PARSE


def after_ai_route(state: dict[str, Any]) -> Literal["apply_ticket_service_node", "error_escalation_node"]:
    if state.get("error_message"):
        return NODE_ERROR_ESCALATE
    return NODE_APPLY_TICKET


def after_ticket_route(state: dict[str, Any]) -> Literal["finalize_node", "error_escalation_node"]:
    if state.get("error_message"):
        return NODE_ERROR_ESCALATE
    return NODE_FINALIZE
