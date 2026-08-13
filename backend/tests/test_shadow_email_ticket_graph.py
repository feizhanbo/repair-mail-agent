from __future__ import annotations

import pytest

from app.workflows.email_ticket.graph import build_shadow_email_ticket_graph
from app.workflows.email_ticket.runner import _legacy_summary
from app.workflows.email_ticket.state import EmailTicketRuntime


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _snapshot(**overrides):
    value = {
        "id": 11,
        "thread_id": 22,
        "ticket_id": None,
        "ticket_status": None,
        "subject": "Repair request",
        "latest_reply_segment": "SN001 cannot start",
        "clean_body": "quoted history",
        "attachments": [],
        "parse_result": {
            "intent_type": "new_repair",
            "handling_level": "auto_repair",
            "confidence_score": 0.95,
            "missing_fields": {},
            "conflict_fields": {},
        },
    }
    value.update(overrides)
    return value


async def _run(snapshot):
    calls: list[int] = []

    async def load_email(email_id: int):
        calls.append(email_id)
        return snapshot

    result = await build_shadow_email_ticket_graph().ainvoke(
        {"email_id": 11, "execution_id": "shadow-1", "route_history": []},
        context=EmailTicketRuntime(load_email=load_email),
    )
    return result, calls


@pytest.mark.anyio
async def test_complete_repair_stops_before_deterministic_validation_side_effects() -> None:
    result, calls = await _run(_snapshot())

    assert calls == [11]
    assert result["shadow_outcome"] == "deterministic_validation_required"
    assert result["validation_plan"]["reasons"] == [
        "SN_LOOKUP_REQUIRED",
        "CUSTOMER_MATCH_REQUIRED",
        "POLICY_VALIDATION_REQUIRED",
    ]
    assert result["execution_state"] == "shadow_completed"


@pytest.mark.anyio
async def test_missing_information_routes_to_followup_plan() -> None:
    snapshot = _snapshot()
    snapshot["parse_result"]["missing_fields"] = {"sn": "required"}

    result, _ = await _run(snapshot)

    assert result["shadow_outcome"] == "customer_followup_required"


@pytest.mark.anyio
async def test_low_confidence_and_conflict_route_to_human() -> None:
    snapshot = _snapshot()
    snapshot["parse_result"].update(confidence_score=0.4, conflict_fields={"customer": "mismatch"})

    result, _ = await _run(snapshot)

    assert result["shadow_outcome"] == "human_review_required"


@pytest.mark.anyio
async def test_unsafe_attachment_routes_to_human_before_classifier() -> None:
    snapshot = _snapshot(
        attachments=[{"id": 7, "parse_status": "needs_manual_review", "blocks_ticket_flow": True}]
    )
    classifier_called = False

    async def load_email(_email_id: int):
        return snapshot

    async def classifier(_payload):
        nonlocal classifier_called
        classifier_called = True
        raise AssertionError("blocking attachment must route before AI")

    result = await build_shadow_email_ticket_graph().ainvoke(
        {"email_id": 11, "execution_id": "shadow-2", "route_history": []},
        context=EmailTicketRuntime(load_email=load_email, classify_email=classifier),
    )

    assert classifier_called is False
    assert result["shadow_outcome"] == "human_review_required"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("intent", "level"),
    [("irrelevant", "unknown"), ("invoice", "lifecycle_only")],
)
async def test_non_automated_intents_finish_without_ticket_automation(intent: str, level: str) -> None:
    snapshot = _snapshot()
    snapshot["parse_result"].update(intent_type=intent, handling_level=level)

    result, _ = await _run(snapshot)

    assert result["shadow_outcome"] == "terminal_without_ticket_automation"


@pytest.mark.anyio
async def test_loader_failure_becomes_sanitized_graph_error() -> None:
    async def load_email(_email_id: int):
        raise TimeoutError("database connection secret must not enter state")

    result = await build_shadow_email_ticket_graph().ainvoke(
        {"email_id": 11, "execution_id": "shadow-3", "route_history": []},
        context=EmailTicketRuntime(load_email=load_email),
    )

    assert result["shadow_outcome"] == "workflow_error"
    assert result["error"] == {
        "code": "TimeoutError",
        "stage": "load_ingested_email",
        "retryable": True,
    }
    assert "secret" not in str(result)


def test_legacy_summary_keeps_only_business_comparison_fields() -> None:
    result = _legacy_summary(
        {
            "parse": {
                "status": "ready_for_export",
                "intent_type": "new_repair",
                "ticket": {"id": 44, "customer_secret": "must-not-copy"},
                "export_validation": {"status": "ready_for_export", "snapshot": "large"},
                "raw_email": "must-not-copy",
            }
        }
    )

    assert result == {
        "outcome": "ready_for_export",
        "intent_type": "new_repair",
        "ticket_id": 44,
        "validation_outcome": "ready_for_export",
    }
