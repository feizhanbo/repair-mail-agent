from __future__ import annotations

from typing import Any, TypedDict


class EmailRepairState(TypedDict, total=False):
    email_id: int
    user_id: int | None
    reason: str

    graph_run_id: str
    current_node: str
    error_message: str | None

    intent_type: str | None
    classification_confidence: float | None
    classification_reason: str | None
    rule_parse_result: dict[str, Any] | None
    ai_parse_result: dict[str, Any] | None

    missing_fields: dict[str, Any] | None
    conflict_fields: dict[str, Any] | None
    confidence_score: float | None

    ticket_id: int | None
    ticket_no: str | None
    current_status_code: str | None

    manual_review_task_id: int | None
    manual_review_reason: str | None

    reply_record_id: int | None

    skip_ai: bool
    requires_manual: bool
    should_create_reply: bool
