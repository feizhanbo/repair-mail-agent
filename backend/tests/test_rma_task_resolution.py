from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from app.models import ManualReviewTask
from app.services import notifications, replies


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _TaskSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _ScalarRows(self.rows)


def test_rma_success_resolves_stale_rma_stage_tasks(monkeypatch) -> None:
    task = ManualReviewTask(
        id=71,
        ticket_id=40,
        task_type="rma_state_invalid",
        status="pending",
    )
    session = _TaskSession([task])
    resolve_notifications = AsyncMock()
    monkeypatch.setattr(
        notifications,
        "resolve_notifications_for_target",
        resolve_notifications,
    )

    count = asyncio.run(
        replies.resolve_completed_rma_tasks(
            session,
            ticket_id=40,
            user_id=1,
        )
    )

    assert count == 1
    assert task.status == "resolved"
    assert task.resolved_by_user_id == 1
    assert task.resolved_at is not None
    assert task.resolution
    resolve_notifications.assert_awaited_once_with(
        session,
        target_type="manual_review_task",
        target_id=71,
    )
