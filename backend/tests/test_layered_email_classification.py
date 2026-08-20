from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.core.email_classification import (
    AUTO_INTENTS,
    CLASSIFICATION_VERSION,
    LIFECYCLE_INTENTS,
    MANUAL_INTENTS,
    HandlingLevel,
    classification_catalog,
    decision_for_intent,
)
from app.models import Email, ManualReviewTask, RepairTicket
from app.services.emails import _contextual_intent
from app.services import manual_review
from app.services.parser import classify_email
from app.services.workflow import transition_ticket


def _email(subject: str, body: str, *, reply: bool = False) -> Email:
    return Email(
        mailbox_account="rmatest1@accotest.com",
        message_id=f"<{abs(hash((subject, body)))}@accotest.com>",
        from_address="rmatest2@accotest.com",
        subject=subject,
        clean_body=body,
        latest_reply_segment=body,
        in_reply_to="<root@accotest.com>" if reply else None,
    )


def test_catalog_contains_every_intent_once_and_all_four_levels() -> None:
    catalog = classification_catalog()
    assert len(catalog) == 14
    assert len({row["intent_type"] for row in catalog}) == len(catalog)
    assert {row["handling_level"] for row in catalog} == {
        "auto_repair", "manual_rma_business", "lifecycle_only", "unknown"
    }
    assert CLASSIFICATION_VERSION == "rma-layered-v1"
    assert len(AUTO_INTENTS) == 3
    assert len(MANUAL_INTENTS) == 4
    assert len(LIFECYCLE_INTENTS) == 6


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("保修确认", "请确认 SN ABC123 是否已经过保？", "warranty_status_inquiry"),
        ("现场服务", "请安排工程师到场处理", "onsite_service"),
        ("器件替换", "本次需要元器件更换", "component_replacement_repair"),
        ("发票", "请确认开票信息", "invoice"),
        ("收货", "待修设备已收到，请入库", "device_intake_received"),
    ],
)
def test_rule_classifier_routes_known_business_types(subject: str, body: str, expected: str) -> None:
    intent, confidence, reason = classify_email(_email(subject, body), body)
    assert intent == expected
    assert confidence >= 0.9
    assert reason


def test_declarative_out_of_warranty_repair_is_first_not_warranty_inquiry() -> None:
    body = "设备已经过保，需要报修。SN: ABC123，故障为无法开机。"
    intent, _, _ = classify_email(_email("设备报修", body), body)
    assert intent == "new_repair"


def test_reply_chain_new_repair_and_supplement_use_deterministic_context() -> None:
    closed = RepairTicket(id=1, current_status_code="closed", ticket_category="standard_repair")
    waiting = RepairTicket(id=2, current_status_code="need_customer_info", ticket_category="standard_repair")

    new_email = _email("Re: 历史维修", "另外还有一台设备需要维修，SN ABC999", reply=True)
    intent, _ = _contextual_intent(new_email, "new_repair", closed)
    assert intent == "thread_new_repair"

    supplement_email = _email("Re: 请补充", "补充 SN ABC123", reply=True)
    intent, _ = _contextual_intent(supplement_email, "customer_supplement", waiting)
    assert intent == "customer_supplement"


def test_second_and_third_decisions_have_distinct_actions() -> None:
    second = decision_for_intent("warranty_status_inquiry")
    third = decision_for_intent("invoice")
    unknown = decision_for_intent("not-in-taxonomy")
    assert second.handling_level == HandlingLevel.MANUAL_RMA_BUSINESS
    assert third.handling_level == HandlingLevel.LIFECYCLE_ONLY
    assert unknown.handling_level == HandlingLevel.UNKNOWN


@pytest.mark.anyio
async def test_manual_business_ticket_cannot_enter_sap_or_rma_chain() -> None:
    ticket = SimpleNamespace(
        id=10,
        current_status_code="manual_review",
        ticket_category="manual_business",
    )
    with pytest.raises(HTTPException) as caught:
        await transition_ticket(
            SimpleNamespace(),
            ticket=ticket,
            to_status_code="ready_for_export",
            trigger_event="manual_resolved",
        )
    assert caught.value.detail == "MANUAL_BUSINESS_RMA_TRANSITION_FORBIDDEN"


@pytest.mark.anyio
async def test_email_level_unknown_task_can_finish_without_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    task = ManualReviewTask(
        id=12,
        ticket_id=None,
        email_id=33,
        thread_id=7,
        task_type="unknown_email_classification",
        priority="high",
        status="claimed",
    )
    monkeypatch.setattr(manual_review, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(manual_review, "log_operation", AsyncMock())
    monkeypatch.setattr(manual_review, "resolve_notifications_for_target", AsyncMock())
    session = SimpleNamespace(flush=AsyncMock())

    result = await manual_review.resolve_task(
        session,
        task_id=12,
        user_id=5,
        resolution="人工确认并通过现有渠道完成。",
        next_action="finish_external_handling",
    )

    assert task.status == "resolved"
    assert result["ticket"] is None


@pytest.mark.anyio
async def test_promote_to_first_requires_explicit_first_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    task = ManualReviewTask(id=13, ticket_id=None, email_id=34, task_type="unknown_mail_classification", status="claimed")
    monkeypatch.setattr(manual_review, "get_task", AsyncMock(return_value=task))
    with pytest.raises(HTTPException) as caught:
        await manual_review.resolve_task(
            SimpleNamespace(), task_id=13, user_id=5, resolution="晋升", next_action="promote_to_first"
        )
    assert caught.value.detail == "TARGET_FIRST_INTENT_REQUIRED"


@pytest.mark.anyio
async def test_promote_to_first_is_idempotent_after_locked_business_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    task = ManualReviewTask(id=14, ticket_id=None, email_id=35, task_type="unknown_mail_classification", status="claimed")
    email = Email(
        id=35,
        persistence_tier="business",
        classification_locked=True,
        mailbox_account="rma@example.com",
        message_id="<promoted@example.com>",
        from_address="customer@example.com",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=email), flush=AsyncMock())
    monkeypatch.setattr(manual_review, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(manual_review, "log_operation", AsyncMock())
    monkeypatch.setattr(manual_review, "resolve_notifications_for_target", AsyncMock())
    monkeypatch.setattr(manual_review, "download_oss_object_bytes", AsyncMock(side_effect=AssertionError("must not download again")))
    result = await manual_review.resolve_task(
        session,
        task_id=14,
        user_id=5,
        resolution="重复提交",
        next_action="promote_to_first",
        target_first_intent="new_repair",
    )
    assert result["reparse_result"]["status"] == "already_promoted"
    assert task.status == "resolved"
