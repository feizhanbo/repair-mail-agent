from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.notifications import event_requires_attention, list_user_notification_center


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _Rows(self.rows)


def _event(event_id: int, *, ticket_id: int, priority: str, created_at: datetime, title: str):
    return SimpleNamespace(
        id=event_id,
        ticket_id=ticket_id,
        target_type="repair_ticket",
        target_id=ticket_id,
        event_type="ticket_system_error",
        priority=priority,
        created_at=created_at,
        title=title,
        content=title,
    )


@pytest.mark.anyio
async def test_notification_center_groups_one_card_per_ticket_and_prefers_high_priority() -> None:
    first = datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)
    second = datetime(2026, 8, 3, 2, 0, tzinfo=timezone.utc)
    owner = SimpleNamespace(username="operator7", real_name="操作员 7")
    rows = [
        (_event(1, ticket_id=10, priority="normal", created_at=second, title="普通问题"), SimpleNamespace(status="unread", user_id=7), SimpleNamespace(ticket_no="RMA-10"), owner),
        (_event(2, ticket_id=10, priority="high", created_at=first, title="高优先级问题"), SimpleNamespace(status="read", user_id=7), SimpleNamespace(ticket_no="RMA-10"), owner),
        (_event(3, ticket_id=11, priority="normal", created_at=second, title="另一张工单"), SimpleNamespace(status="unread", user_id=7), SimpleNamespace(ticket_no="RMA-11"), owner),
    ]

    result = await list_user_notification_center(_Session(rows), user_id=7, page_size=20)

    assert result["total"] == 2
    assert result["unread_total"] == 2
    first_item = next(item for item in result["items"] if item["ticket_id"] == 10)
    assert first_item["title"] == "高优先级问题"
    assert first_item["active_event_count"] == 2
    assert first_item["unread_event_count"] == 1
    assert first_item["state_user_id"] == 7
    assert first_item["state_username"] == "operator7"


@pytest.mark.anyio
async def test_admin_center_does_not_merge_different_users_for_same_ticket() -> None:
    created_at = datetime(2026, 8, 3, 2, 0, tzinfo=timezone.utc)
    event = _event(1, ticket_id=10, priority="normal", created_at=created_at, title="同一工单")
    rows = [
        (event, SimpleNamespace(status="unread", user_id=7), SimpleNamespace(ticket_no="RMA-10"), SimpleNamespace(username="a", real_name="A")),
        (event, SimpleNamespace(status="read", user_id=8), SimpleNamespace(ticket_no="RMA-10"), SimpleNamespace(username="b", real_name="B")),
    ]

    result = await list_user_notification_center(_Session(rows), user_id=None, page_size=20)

    assert result["total"] == 2
    assert {item["state_user_id"] for item in result["items"]} == {7, 8}


def test_notification_event_policy_keeps_success_messages_out_of_center() -> None:
    assert event_requires_attention("sap_export_failed") is True
    assert event_requires_attention("manual_review_assigned") is True
    assert event_requires_attention("sap_export_accepted") is False
    assert event_requires_attention("rma_reply_sent") is False
    assert event_requires_attention("future_informational_event") is False
