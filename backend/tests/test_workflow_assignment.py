from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import notifications as notification_api
from app.models import ManualReviewTask, User
from app.services import notifications, workflow


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Result:
    def scalars(self):
        return self

    def first(self):
        return None


class Session:
    def __init__(self, owner: User | None) -> None:
        self.owner = owner
        self.added = []

    async def execute(self, _statement):
        return Result()

    async def scalar(self, _statement):
        return self.owner

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = len(self.added)


@pytest.mark.anyio
async def test_manual_task_is_pending_with_system_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session(User(id=11, username="miya", status="active"))
    notices: list[dict] = []

    async def fake_notification(_session, **kwargs):
        notices.append(kwargs)

    monkeypatch.setattr(workflow, "create_notification", fake_notification)
    ticket = SimpleNamespace(id=8, ticket_no="RMA-TEST", source_email_id=3, assigned_user_id=11)

    task = await workflow.create_manual_task_if_missing(session, ticket=ticket, task_type="manual")

    assert isinstance(task, ManualReviewTask)
    assert task.status == "pending"
    assert task.assigned_user_id == 11
    assert task.claimed_by_user_id is None
    assert notices[0]["event_type"] == "manual_review_assigned"
    assert notices[0]["recipient_user_id"] == 11


@pytest.mark.anyio
async def test_missing_required_operator_stays_visible_and_notifies_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session(None)
    notices: list[dict] = []

    async def fake_notification(_session, **kwargs):
        notices.append(kwargs)

    monkeypatch.setattr(workflow, "create_notification", fake_notification)
    ticket = SimpleNamespace(id=9, ticket_no="RMA-FAILED", source_email_id=4, assigned_user_id=None)

    task = await workflow.create_manual_task_if_missing(session, ticket=ticket, task_type="manual")

    assert task.status == "pending"
    assert task.assigned_user_id is None
    assert notices[0]["event_type"] == "manual_review_assignment_failed"
    assert notices[0]["recipient_role_code"] == "admin"


@pytest.mark.anyio
async def test_resolved_notification_cannot_be_changed_back_to_read(monkeypatch: pytest.MonkeyPatch) -> None:
    event = SimpleNamespace(id=7)
    state = SimpleNamespace(status="resolved", read_at=None)

    async def fake_get(*_args, **_kwargs):
        return event, state

    monkeypatch.setattr(notifications, "get_user_notification", fake_get)
    returned = await notifications.mark_user_notification_read(SimpleNamespace(), notification_id=7, user_id=2)

    assert returned == (event, state)
    assert state.status == "resolved"
    assert state.read_at is not None


@pytest.mark.anyio
async def test_missing_user_notification_state_returns_explicit_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(notification_api, "mark_user_notification_read", missing)
    with pytest.raises(HTTPException) as exc_info:
        await notification_api.read_notification(7, SimpleNamespace(), SimpleNamespace(id=2))

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "NOTIFICATION_NOT_FOUND"


@pytest.mark.anyio
async def test_manual_review_cannot_leave_while_another_task_is_open() -> None:
    class BlockingSession:
        async def scalar(self, _statement):
            return 42

    ticket = SimpleNamespace(id=8, current_status_code="manual_review")
    with pytest.raises(HTTPException) as exc_info:
        await workflow.transition_ticket(
            BlockingSession(),
            ticket=ticket,
            to_status_code="need_customer_info",
            trigger_event="manual_resolved",
            resolving_task_id=7,
        )
    assert exc_info.value.detail == "MANUAL_TASKS_UNRESOLVED"
    assert ticket.current_status_code == "manual_review"
