from __future__ import annotations

NODE_LOAD_EMAIL = "load_email_node"
NODE_RULE_EXTRACT = "rule_extract_node"
NODE_AI_FULL_PARSE = "ai_full_parse_node"
NODE_APPLY_TICKET = "apply_ticket_service_node"
NODE_FINALIZE = "finalize_node"
NODE_ERROR_ESCALATE = "error_escalation_node"

NODE_STATUS_PENDING = "pending"
NODE_STATUS_RUNNING = "running"
NODE_STATUS_COMPLETED = "completed"
NODE_STATUS_FAILED = "failed"

GRAPH_STATUS_RUNNING = "running"
GRAPH_STATUS_COMPLETED = "completed"
GRAPH_STATUS_FAILED = "failed"
GRAPH_STATUS_INTERRUPTED = "interrupted"

SKIPPABLE_INTENTS = {"irrelevant", "unknown"}

BUSINESS_INTENTS = {
    "new_repair", "customer_reply", "customer_receipt_confirmed",
    "internal_forward", "rma_request", "replacement_request",
    "update_repair", "shipment_notification", "contract_confirmation",
    "forward_email", "rma_notification", "repair_shipment_notification",
}
