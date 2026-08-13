from __future__ import annotations

import pytest

from app.workflows.email_ticket import routers


@pytest.mark.parametrize(
    ("router", "state", "expected"),
    [
        (routers.route_load_result, {}, "normalize"),
        (routers.route_load_result, {"error": {"code": "LOAD_FAILED"}}, "error"),
        (routers.route_attachment_result, {"attachment_results": []}, "classify"),
        (routers.route_attachment_result, {"attachment_results": [{"parse_status": "needs_manual_review"}]}, "human"),
        (routers.route_attachment_result, {"attachment_results": [{"parse_status": "unsupported"}]}, "human"),
        (routers.route_attachment_result, {"attachment_results": [{"parse_status": "failed"}]}, "human"),
        (routers.route_attachment_result, {"attachment_results": [{"parse_status": "parsed", "blocks_ticket_flow": True}]}, "human"),
        (routers.route_intent, {"ai_result": {"intent_type": "irrelevant"}}, "terminal"),
        (routers.route_intent, {"ai_result": {"intent_type": "new_repair", "handling_level": "auto_repair"}}, "auto"),
        (routers.route_intent, {"ai_result": {"intent_type": "device_intake_received", "handling_level": "lifecycle_only"}}, "terminal"),
        (routers.route_intent, {"ai_result": {"intent_type": "unknown", "handling_level": "unknown"}}, "human"),
        (routers.route_parse_quality, {"validation_plan": {"outcome": "request_customer_info"}}, "followup"),
        (routers.route_parse_quality, {"validation_plan": {"outcome": "deterministic_validation_required"}}, "validate"),
        (routers.route_parse_quality, {"validation_plan": {"outcome": "human_review"}}, "human"),
        (routers.route_validation_result, {"validation_result": {"status": "ready_for_export", "sap_required": True}}, "sap"),
        (routers.route_validation_result, {"validation_result": {"status": "ready_for_export", "sap_required": False}}, "complete"),
        (routers.route_validation_result, {"validation_result": {"status": "need_customer_info"}}, "followup"),
        (routers.route_validation_result, {"validation_result": {"status": "missing_fields"}}, "followup"),
        (routers.route_validation_result, {"validation_result": {"status": "unknown"}}, "human"),
        (routers.route_sap_result, {"sap_result": {"status": "waiting_sap_result"}}, "wait"),
        (routers.route_sap_result, {"sap_result": {"status": "waiting_rma"}}, "wait"),
        (routers.route_sap_result, {"sap_result": {"status": "rma_received"}}, "wait"),
        (routers.route_sap_result, {"sap_result": {"status": "submit_unknown"}}, "reconcile"),
        (routers.route_sap_result, {"sap_result": {"status": "pending"}, "sap_submit_attempt_count": 1}, "submit"),
        (routers.route_sap_result, {"sap_result": {"status": "pending"}, "sap_submit_attempt_count": 2}, "human"),
        (routers.route_sap_result, {"sap_result": {"status": "submit_failed"}}, "human"),
        (routers.route_sap_poll_result, {"sap_result": {"status": "rma_received"}}, "rma"),
        (routers.route_sap_poll_result, {"sap_result": {"status": "waiting_rma"}}, "wait"),
        (routers.route_sap_poll_result, {"sap_result": {"status": "submit_unknown"}}, "wait"),
        (routers.route_sap_poll_result, {"sap_result": {"status": "pending"}, "sap_submit_attempt_count": 1}, "submit"),
        (routers.route_sap_poll_result, {"sap_result": {"status": "pending"}, "sap_submit_attempt_count": 2}, "human"),
        (routers.route_sap_poll_result, {"sap_result": {"status": "timed_out"}}, "human"),
        (routers.route_rma_prepare_result, {"rma_result": {"status": "prepared"}}, "send"),
        (routers.route_rma_prepare_result, {"rma_result": {"status": "approved_pending_send"}}, "send"),
        (routers.route_rma_prepare_result, {"rma_result": {"status": "sent"}}, "archive"),
        (routers.route_rma_prepare_result, {"rma_result": {"status": "failed"}}, "human"),
        (routers.route_rma_send_result, {"send_result": {"status": "sent"}}, "archive"),
        (routers.route_rma_send_result, {"send_result": {"status": "send_uncertain"}}, "human"),
        (routers.route_rma_archive_result, {"archive_result": {"status": "closed"}}, "complete"),
        (routers.route_rma_archive_result, {"archive_result": {"status": "archive_failed"}}, "human"),
        (routers.route_human_result, {"ticket_id": 7, "human_result": {"action": "validate"}}, "validate"),
        (routers.route_human_result, {"human_result": {"action": "validate"}}, "human"),
        (routers.route_human_result, {"human_result": {"action": "reparse"}}, "reparse"),
        (
            routers.route_human_result,
            {"ticket_id": 7, "human_result": {"action": "request_customer_info"}},
            "prepare_reply",
        ),
        (routers.route_human_result, {"human_result": {"action": "approve_send"}, "rma_result": {"reply_id": 9}}, "send_rma"),
        (
            routers.route_human_result,
            {"human_result": {"action": "approve_send"}, "reply_result": {"reply_id": 10}},
            "send_reply",
        ),
        (routers.route_human_result, {"human_result": {"action": "approve_send"}}, "human"),
        (
            routers.route_human_result,
            {"human_result": {"action": "reject_send"}, "reply_result": {"reply_id": 10}},
            "terminal",
        ),
        (routers.route_human_result, {"human_result": {"action": "reject_send"}}, "human"),
        (routers.route_human_result, {"human_result": {"action": "request_customer_info"}}, "human"),
        (routers.route_human_result, {"human_result": {"action": "close"}}, "terminal"),
        (routers.route_human_result, {"human_result": {"action": "unexpected"}}, "human"),
        (routers.route_reply_result, {"reply_result": {"status": "prepared", "reply_id": 20, "send_status": "approved_pending_send"}}, "send"),
        (routers.route_reply_result, {"reply_result": {"status": "prepared", "reply_id": 20, "send_status": "pending_review"}}, "human"),
        (routers.route_reply_result, {"reply_result": {"status": "prepared", "reply_id": 20, "send_status": "unknown"}}, "human"),
        (routers.route_reply_result, {"reply_result": {"status": "failed"}}, "human"),
        (routers.route_reply_send_result, {"send_result": {"status": "sent"}}, "complete"),
        (routers.route_reply_send_result, {"send_result": {"status": "send_uncertain"}}, "human"),
        (routers.route_adoption_result, {"adoption_result": {"email_parse_status": "parsed", "ticket_id": 4}}, "validate"),
        (routers.route_adoption_result, {"adoption_result": {"email_parse_status": "parsed", "ticket_id": 4, "missing_fields": {"sn": True}}}, "followup"),
        (routers.route_adoption_result, {"adoption_result": {"email_parse_status": "skipped"}}, "terminal"),
        (routers.route_adoption_result, {"adoption_result": {"email_parse_status": "failed"}}, "human"),
        (routers.route_error_or_next, {}, "next"),
        (routers.route_error_or_next, {"error": {"code": "FAILED"}}, "human"),
    ],
)
def test_router_branch_contract(router, state: dict, expected: str) -> None:
    assert router(state) == expected


@pytest.mark.parametrize(
    "router",
    [
        routers.route_attachment_result,
        routers.route_intent,
        routers.route_parse_quality,
        routers.route_validation_result,
        routers.route_sap_result,
        routers.route_sap_poll_result,
        routers.route_rma_prepare_result,
        routers.route_rma_send_result,
        routers.route_rma_archive_result,
        routers.route_human_result,
        routers.route_reply_result,
        routers.route_reply_send_result,
        routers.route_adoption_result,
    ],
)
def test_router_recoverable_error_short_circuits_to_human(router) -> None:
    assert router({"error": {"code": "RECOVERABLE"}}) == "human"
