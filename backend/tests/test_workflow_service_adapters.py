from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import Email, RepairTicket
from app.workflows.email_ticket import adapters, routers
from app.workflows.email_ticket.human import allowed_human_actions


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_active_adapter_delegates_validation_and_sap_without_reimplementing(monkeypatch) -> None:
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    services = adapters.ActiveWorkflowServices(session)
    validate = AsyncMock(return_value={"status": "ready_for_export"})
    submit = AsyncMock(return_value={"status": "waiting_sap_result"})
    reconcile = AsyncMock(return_value={"status": "waiting_sap_result"})
    monkeypatch.setattr(adapters, "validate_and_mark_ready_for_export", validate)
    monkeypatch.setattr(adapters, "submit_export_batch", submit)
    monkeypatch.setattr(adapters, "reconcile_uncertain_submission", reconcile)

    assert await services.validate_ticket({"ticket_id": 1}) == {"status": "ready_for_export"}
    assert await services.submit_sap(2) == {"status": "waiting_sap_result"}
    assert await services.reconcile_sap(2) == {"status": "waiting_sap_result"}
    validate.assert_awaited_once_with(
        session,
        ticket_id=1,
        user_id=None,
        resolving_task_id=None,
        enqueue_relay_job=False,
    )
    submit.assert_awaited_once_with(session, export_id=2, schedule_jobs=False)
    reconcile.assert_awaited_once_with(
        session,
        export_id=2,
        reason="langgraph_uncertain_submit_reconciliation",
        user_id=None,
        schedule_jobs=False,
    )
    assert session.commit.await_count == 3
    session.rollback.assert_not_awaited()


@pytest.mark.anyio
async def test_active_adapter_reuses_existing_ticket_human_task_service(monkeypatch) -> None:
    ticket = RepairTicket(id=3, ticket_no="RMA-3", current_status_code="manual_review")

    class Session:
        def __init__(self) -> None:
            self.commit = AsyncMock()

        async def get(self, model, identity):
            assert model is RepairTicket and identity == 3
            return ticket

    create = AsyncMock(return_value=SimpleNamespace(id=8))
    monkeypatch.setattr(adapters, "create_manual_task_if_missing", create)
    session = Session()
    services = adapters.ActiveWorkflowServices(session)

    task_id = await services.create_human_task(
        {"ticket_id": 3, "email_id": 4, "reasons": ["CUSTOMER_CONFLICT"]}
    )

    assert task_id == 8
    create.assert_awaited_once()
    assert create.await_args.kwargs["task_type"] == "langgraph_human_review"
    session.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_active_adapter_rolls_back_before_retrying_failed_node(monkeypatch) -> None:
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(
        adapters,
        "validate_and_mark_ready_for_export",
        AsyncMock(side_effect=RuntimeError("DATABASE_UNAVAILABLE")),
    )

    with pytest.raises(RuntimeError, match="DATABASE_UNAVAILABLE"):
        await adapters.ActiveWorkflowServices(session).validate_ticket({"ticket_id": 1})

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_validate_adapter_passes_human_resolving_task_id(monkeypatch) -> None:
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    validate = AsyncMock(return_value={"status": "ready_for_export"})
    monkeypatch.setattr(adapters, "validate_and_mark_ready_for_export", validate)
    services = adapters.ActiveWorkflowServices(session)

    result = await services.validate_ticket({"ticket_id": 1, "resolving_task_id": 7})

    assert result == {"status": "ready_for_export"}
    validate.assert_awaited_once_with(
        session,
        ticket_id=1,
        user_id=None,
        resolving_task_id=7,
        enqueue_relay_job=False,
    )
    session.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_active_adapter_disables_legacy_scheduling_for_graph_poll(monkeypatch) -> None:
    """Graph poll must neither schedule a legacy relay job nor an RMA job."""
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    poll = AsyncMock(return_value={"status": "pending", "export_id": 2})
    monkeypatch.setattr(adapters, "poll_export_batch", poll)
    services = adapters.ActiveWorkflowServices(session)

    assert await services.poll_sap(2) == {"status": "pending", "export_id": 2}
    poll.assert_awaited_once_with(
        session,
        export_id=2,
        schedule_jobs=False,
        enqueue_rma_job=False,
    )
    session.commit.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("service_result", "expected_reply_id", "expected_send_status", "expected_route"),
    [
        (
            # Brand-new draft created with auto-send enabled and send_immediately=False.
            {"status": "prepared", "ticket_id": 10, "reply_id": 20, "pdf_oss_object_id": None, "idempotent_reuse": False},
            20,
            "approved_pending_send",
            "send",
        ),
        (
            # Brand-new draft created for manual review.
            {"status": "pending_review", "ticket_id": 10, "reply_id": 21, "idempotent_reuse": False},
            21,
            "pending_review",
            "human",
        ),
        (
            # Reused draft returned as a flat serialize_reply.
            {"id": 30, "send_status": "pending_review", "reply_type": "receipt"},
            30,
            "pending_review",
            "human",
        ),
        (
            {"id": 31, "send_status": "approved_pending_send", "reply_type": "receipt"},
            31,
            "approved_pending_send",
            "send",
        ),
    ],
)
async def test_reply_prepare_adapter_normalizes_service_shapes_for_router(
    monkeypatch,
    service_result: dict,
    expected_reply_id: int,
    expected_send_status: str,
    expected_route: str,
) -> None:
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    create = AsyncMock(return_value=service_result)
    monkeypatch.setattr(adapters, "create_reply_draft", create)
    services = adapters.ActiveWorkflowServices(session)

    canonical = await services.prepare_reply(
        {"ticket_id": 10, "email_id": 3, "reply_type": "receipt", "missing_fields": {}}
    )

    assert canonical["status"] == "prepared"
    assert canonical["reply_id"] == expected_reply_id
    assert canonical["send_status"] == expected_send_status
    assert routers.route_reply_result({"reply_result": canonical}) == expected_route
    if expected_send_status == "pending_review":
        actions = allowed_human_actions({"ticket_id": 10, "reply_result": canonical})
        assert {"approve_send", "reject_send"} <= set(actions)
    create.assert_awaited_once()
    assert create.await_args.kwargs["send_immediately"] is False
    session.commit.assert_awaited_once()
