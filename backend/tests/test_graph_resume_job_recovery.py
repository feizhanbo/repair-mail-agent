from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import jobs
from app.workflows import executions


class _PairResult:
    def __init__(self, pair):
        self._pair = pair

    def first(self):
        return self._pair

    def all(self):
        return [self._pair] if self._pair is not None else []


@pytest.mark.anyio
async def test_terminal_graph_resume_releases_interrupt_and_human_task() -> None:
    job = SimpleNamespace(
        job_type="graph_resume",
        metadata_json={"execution_id": "exec-1", "interrupt_id": "interrupt-1"},
    )
    execution = SimpleNamespace(status="resume_queued", last_error_code=None)
    interrupt = SimpleNamespace(
        status="resume_queued",
        manual_task_id=9,
        error_message=None,
        request_payload={"allowed_actions": ["validate", "reparse", "close"]},
    )
    task = SimpleNamespace(
        status="resolved",
        resolved_by_user_id=5,
        resolved_at=object(),
        resolution="approved",
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_PairResult((execution, interrupt))),
        get=AsyncMock(return_value=task),
    )

    released = await jobs.release_terminal_graph_resume(
        session,
        job=job,
        error_code="POSTGRES_UNAVAILABLE",
    )

    assert released is True
    assert interrupt.status == "pending"
    assert interrupt.error_message == "POSTGRES_UNAVAILABLE"
    assert execution.status == "waiting_human"
    assert execution.last_error_code == "POSTGRES_UNAVAILABLE"
    assert task.status == "pending"
    assert task.resolved_by_user_id is None
    assert task.resolved_at is None


@pytest.mark.anyio
async def test_terminal_external_resume_releases_interrupt_without_faking_human_task() -> None:
    job = SimpleNamespace(
        job_type="graph_resume",
        metadata_json={"execution_id": "exec-2", "interrupt_id": "interrupt-2"},
    )
    execution = SimpleNamespace(status="resume_queued", last_error_code=None)
    interrupt = SimpleNamespace(
        status="pending",
        manual_task_id=None,
        error_message=None,
        request_payload={"allowed_actions": ["close"]},
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_PairResult((execution, interrupt))),
        get=AsyncMock(),
    )

    assert await jobs.release_terminal_graph_resume(
        session, job=job, error_code="POSTGRES_UNAVAILABLE"
    )
    assert interrupt.status == "pending"
    assert execution.status == "waiting_external"
    session.get.assert_not_awaited()


@pytest.mark.anyio
async def test_explicit_retry_reactivates_same_terminal_resume_job(monkeypatch) -> None:
    existing = SimpleNamespace(
        id=11,
        status="failed",
        max_attempts=3,
        attempt_count=3,
        next_run_at=object(),
        finished_at=object(),
        error_code="POSTGRES_UNAVAILABLE",
        error_message="RuntimeError",
        metadata_json={"old": True},
        updated_at=None,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=existing))

    result = await jobs.enqueue_job(
        session,
        job_type="graph_resume",
        resource_type="workflow_execution",
        resource_id=5,
        idempotency_key="graph_resume:exec-1:interrupt-1",
        metadata={"execution_id": "exec-1", "interrupt_id": "interrupt-1"},
        max_attempts=3,
        reactivate_terminal=True,
    )

    assert result is existing
    assert existing.status == "queued"
    assert existing.max_attempts == 6
    assert existing.next_run_at is None
    assert existing.finished_at is None
    assert existing.error_code is None
    assert existing.metadata_json == {
        "execution_id": "exec-1",
        "interrupt_id": "interrupt-1",
    }


@pytest.mark.anyio
async def test_normal_idempotent_enqueue_does_not_reactivate_terminal_job() -> None:
    existing = SimpleNamespace(status="failed")
    session = SimpleNamespace(scalar=AsyncMock(return_value=existing))

    result = await jobs.enqueue_job(
        session,
        job_type="email_parse",
        resource_type="email",
        resource_id=5,
        idempotency_key="email-5",
    )

    assert result is existing
    assert existing.status == "failed"


@pytest.mark.anyio
async def test_operator_can_reactivate_pending_external_interrupt(monkeypatch) -> None:
    execution = SimpleNamespace(id=5, execution_id="exec-2", status="waiting_external", last_error_code="FAILED")
    interrupt = SimpleNamespace(
        interrupt_id="interrupt-2",
        status="pending",
        manual_task_id=None,
        response_payload={"reason": "scheduled_poll"},
        error_message="FAILED",
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=_PairResult((execution, interrupt))))
    enqueue = AsyncMock(return_value=SimpleNamespace(id=17))
    monkeypatch.setattr(jobs, "enqueue_job", enqueue)

    result = await executions.retry_pending_external_interrupt(
        session,
        execution_id="exec-2",
        interrupt_id="interrupt-2",
        operator_user_id=9,
    )

    assert result == {
        "execution_id": "exec-2",
        "interrupt_id": "interrupt-2",
        "resume_job_id": 17,
        "status": "resume_queued",
    }
    assert interrupt.status == "resume_queued"
    assert execution.status == "resume_queued"
    assert enqueue.await_args.kwargs["reactivate_terminal"] is True
    assert enqueue.await_args.kwargs["metadata"]["user_id"] == 9


@pytest.mark.anyio
async def test_operator_retry_rejects_human_interrupt(monkeypatch) -> None:
    execution = SimpleNamespace(id=5, execution_id="exec-human")
    interrupt = SimpleNamespace(interrupt_id="interrupt-human", status="pending", manual_task_id=7)
    session = SimpleNamespace(execute=AsyncMock(return_value=_PairResult((execution, interrupt))))

    with pytest.raises(ValueError, match="WORKFLOW_HUMAN_INTERRUPT_REQUIRES_TASK_RESUBMISSION"):
        await executions.retry_pending_external_interrupt(
            session,
            execution_id="exec-human",
            interrupt_id="interrupt-human",
            operator_user_id=9,
        )


@pytest.mark.anyio
async def test_reply_resume_reuses_active_resume_queued_interrupt(monkeypatch) -> None:
    execution = SimpleNamespace(id=5, execution_id="exec-reply", status="resume_queued")
    interrupt = SimpleNamespace(
        interrupt_id="interrupt-reply",
        status="resume_queued",
        manual_task_id=None,
        expected_ticket_version=3,
        request_payload={"reply_id": 41, "allowed_actions": ["approve_send", "close"]},
        response_payload={"task_id": None, "action": "approve_send"},
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_PairResult((execution, interrupt))),
        get=AsyncMock(),
    )
    enqueue = AsyncMock(return_value=SimpleNamespace(id=17))
    monkeypatch.setattr(jobs, "enqueue_job", enqueue)

    result = await executions.enqueue_reply_resume_if_bound(
        session,
        reply_id=41,
        reviewer_id=9,
    )

    assert result == {"execution_id": "exec-reply", "resume_job_id": 17}
    assert enqueue.await_args.kwargs["idempotency_key"] == (
        "graph_resume:exec-reply:interrupt-reply"
    )
    assert interrupt.request_payload["allowed_actions"] == ["approve_send", "close"]


@pytest.mark.anyio
async def test_reply_active_ownership_includes_resume_queued() -> None:
    class Scalars:
        def all(self):
            return [{"reply_id": 41, "allowed_actions": ["approve_send"]}]

    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: Scalars()))
    )

    assert await executions.reply_has_pending_interrupt(session, reply_id=41) is True


@pytest.mark.anyio
async def test_manual_resume_refuses_to_overwrite_already_queued_decision(monkeypatch) -> None:
    execution = SimpleNamespace(id=5, execution_id="exec-manual", status="resume_queued")
    interrupt = SimpleNamespace(
        interrupt_id="interrupt-manual",
        status="resume_queued",
        manual_task_id=9,
        expected_ticket_version=3,
        request_payload={"allowed_actions": ["validate", "reparse", "close"]},
        response_payload={"task_id": 9, "action": "validate"},
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=_PairResult((execution, interrupt))))
    enqueue = AsyncMock()
    monkeypatch.setattr(jobs, "enqueue_job", enqueue)

    with pytest.raises(ValueError, match="WORKFLOW_INTERRUPT_RESUME_ALREADY_QUEUED"):
        await executions.enqueue_manual_resume_if_bound(
            session,
            manual_task_id=9,
            action="reparse",
            edited_fields={},
            reviewer_id=7,
            expected_ticket_version=3,
        )

    enqueue.assert_not_awaited()
    assert interrupt.response_payload == {"task_id": 9, "action": "validate"}


@pytest.mark.anyio
async def test_reply_rejection_queues_terminal_graph_resume(monkeypatch) -> None:
    execution = SimpleNamespace(id=5, execution_id="exec-reject", status="waiting_human")
    interrupt = SimpleNamespace(
        interrupt_id="interrupt-reject",
        status="pending",
        manual_task_id=None,
        expected_ticket_version=3,
        request_payload={"reply_id": 41, "allowed_actions": ["approve_send", "reject_send"]},
        response_payload=None,
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_PairResult((execution, interrupt))),
        get=AsyncMock(),
    )
    enqueue = AsyncMock(return_value=SimpleNamespace(id=19))
    monkeypatch.setattr(jobs, "enqueue_job", enqueue)

    result = await executions.enqueue_reply_resume_if_bound(
        session,
        reply_id=41,
        reviewer_id=9,
        action="reject_send",
    )

    assert result == {"execution_id": "exec-reject", "resume_job_id": 19}
    assert enqueue.await_args.kwargs["metadata"]["response_payload"]["action"] == "reject_send"
    assert interrupt.status == "resume_queued"
    assert execution.status == "resume_queued"
