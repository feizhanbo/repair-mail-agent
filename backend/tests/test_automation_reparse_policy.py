from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1.system import _config_payload
from app.models import ManualReviewTask, ParseResult, RepairTicket
from app.seed import WORKFLOW_TRANSITIONS
from app.services import emails
from app.services.jobs import _job_error_is_retryable


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ScalarSession:
    def __init__(self, value):
        self.value = value

    async def scalar(self, _statement):
        return self.value


@pytest.mark.anyio
async def test_system_reparse_allows_low_confidence_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emails.settings, "AUTO_APPLY_MIN_CONFIDENCE", 0.85)
    previous = ParseResult(
        id=10,
        email_id=1,
        parser_type="ai",
        confidence_score=0.84,
        evidence={"classification_alignment": {"matched": True}},
    )

    assert await emails._require_system_reparse_eligibility(
        ScalarSession(previous), email_id=1
    ) is previous


@pytest.mark.anyio
async def test_system_reparse_allows_intent_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emails.settings, "AUTO_APPLY_MIN_CONFIDENCE", 0.85)
    previous = ParseResult(
        id=11,
        email_id=1,
        parser_type="ai",
        confidence_score=0.95,
        evidence={"classification_alignment": {"matched": False}},
    )

    assert await emails._require_system_reparse_eligibility(
        ScalarSession(previous), email_id=1
    ) is previous


@pytest.mark.anyio
async def test_system_reparse_rejects_clean_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emails.settings, "AUTO_APPLY_MIN_CONFIDENCE", 0.85)
    previous = ParseResult(
        id=12,
        email_id=1,
        parser_type="ai",
        confidence_score=0.95,
        evidence={"classification_alignment": {"matched": True}},
    )

    with pytest.raises(HTTPException) as caught:
        await emails._require_system_reparse_eligibility(
            ScalarSession(previous), email_id=1
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "SYSTEM_REPARSE_NOT_ELIGIBLE"


@pytest.mark.anyio
async def test_recovered_parse_resolves_only_matching_ai_review_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matching = ManualReviewTask(
        id=21,
        ticket_id=7,
        email_id=3,
        task_type="ai_review_required",
        status="pending",
        priority="high",
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [matching]

    class Session:
        async def execute(self, _statement):
            return Result()

    log_operation = AsyncMock()
    resolve_notifications = AsyncMock()
    monkeypatch.setattr(emails, "log_operation", log_operation)
    monkeypatch.setattr(emails, "resolve_notifications_for_target", resolve_notifications)

    resolved_ids = await emails._resolve_recovered_ai_review_tasks(
        Session(),
        ticket=RepairTicket(id=7, ticket_no="RMA-7", current_status_code="manual_review"),
        email_id=3,
        user_id=None,
    )

    assert resolved_ids == [21]
    assert matching.status == "resolved"
    assert matching.resolved_by_user_id is None
    assert matching.resolved_at is not None
    log_operation.assert_awaited_once()
    resolve_notifications.assert_awaited_once()


def test_seed_contains_non_manual_reparse_recovery_transition() -> None:
    transition = next(
        row
        for row in WORKFLOW_TRANSITIONS
        if row["from_status_code"] == "manual_review"
        and row["to_status_code"] == "parsed"
        and row["trigger_event"] == "reparse_validation_passed"
    )

    assert transition.get("require_manual", False) is False


def test_system_info_does_not_expose_removed_relay_push_switch() -> None:
    assert "relay_push_enabled" not in _config_payload()["integrations"]


def test_ineligible_system_reparse_job_is_not_retried() -> None:
    assert _job_error_is_retryable("SYSTEM_REPARSE_NOT_ELIGIBLE") is False


def test_reparse_user_id_remains_optional() -> None:
    assert inspect.signature(emails.reparse_email).parameters["user_id"].default is None
