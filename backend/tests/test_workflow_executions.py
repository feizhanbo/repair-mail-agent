from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

from app.workflows import executions
from app.workflows.email_ticket import runner


def _snapshot(
    *,
    execution_id: str,
    checkpoint_id: str | None,
    checkpoint_step: int | None,
    execution_state: str = "completed",
    interrupts: tuple = (),
    next_nodes: tuple = (),
):
    task = SimpleNamespace(interrupts=interrupts)
    return SimpleNamespace(
        config=(
            {"configurable": {"checkpoint_id": checkpoint_id}}
            if checkpoint_id is not None
            else None
        ),
        metadata={} if checkpoint_step is None else {"step": checkpoint_step},
        values={"execution_id": execution_id, "execution_state": execution_state},
        tasks=(task,) if interrupts else (),
        next=next_nodes,
    )


@pytest.mark.anyio
async def test_create_execution_records_current_workflow_contract() -> None:
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        add=lambda value: setattr(session, "added", value),
        flush=AsyncMock(),
    )

    created = await executions.create_execution(
        session,
        execution_id="exec-1",
        graph_thread_id="email-ticket-exec-1",
        email_id=7,
    )

    assert created is session.added
    assert created.workflow_name == executions.WORKFLOW_NAME
    assert created.workflow_version == "langgraph-v2"
    assert created.state_schema_version == "email-ticket-state-v1"
    assert created.email_id == 7
    assert created.trigger_job_id is None
    session.flush.assert_awaited_once()


@pytest.mark.anyio
async def test_existing_execution_identity_is_immutable() -> None:
    existing = SimpleNamespace(
        workflow_name=executions.WORKFLOW_NAME,
        graph_thread_id="email-ticket-exec-1",
        email_id=7,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=existing))

    reused = await executions.create_execution(
        session,
        execution_id="exec-1",
        graph_thread_id="email-ticket-exec-1",
        email_id=7,
    )
    assert reused is existing

    for conflicting in (
        {"graph_thread_id": "other-thread", "email_id": 7},
        {"graph_thread_id": "email-ticket-exec-1", "email_id": 8},
    ):
        with pytest.raises(ValueError, match="WORKFLOW_EXECUTION_IDENTITY_CONFLICT"):
            await executions.create_execution(
                session,
                execution_id="exec-1",
                **conflicting,
            )


@pytest.mark.anyio
async def test_execution_records_and_fences_trigger_job_identity() -> None:
    existing = SimpleNamespace(
        workflow_name=executions.WORKFLOW_NAME,
        graph_thread_id="email-ticket-exec-1",
        email_id=7,
        trigger_job_id=None,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=existing))

    reused = await executions.create_execution(
        session,
        execution_id="exec-1",
        graph_thread_id="email-ticket-exec-1",
        email_id=7,
        trigger_job_id=21,
    )

    assert reused.trigger_job_id == 21
    with pytest.raises(ValueError, match="WORKFLOW_TRIGGER_JOB_IDENTITY_CONFLICT"):
        await executions.create_execution(
            session,
            execution_id="exec-1",
            graph_thread_id="email-ticket-exec-1",
            email_id=7,
            trigger_job_id=22,
        )


@pytest.mark.anyio
async def test_same_trigger_job_moves_failed_execution_back_to_running() -> None:
    existing = SimpleNamespace(
        workflow_name=executions.WORKFLOW_NAME,
        graph_thread_id="email-ticket-exec-1",
        email_id=7,
        trigger_job_id=21,
        status="failed",
        completed_at=object(),
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=existing))

    reused = await executions.create_execution(
        session,
        execution_id="exec-1",
        graph_thread_id="email-ticket-exec-1",
        email_id=7,
        trigger_job_id=21,
    )

    assert reused.status == "running"
    assert reused.completed_at is None


@pytest.mark.anyio
async def test_execution_id_cannot_be_reused_after_source_email_deletion() -> None:
    deleted_source_execution = SimpleNamespace(
        workflow_name=executions.WORKFLOW_NAME,
        graph_thread_id="email-ticket-exec-deleted",
        email_id=None,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=deleted_source_execution))

    with pytest.raises(ValueError, match="WORKFLOW_EXECUTION_IDENTITY_CONFLICT"):
        await executions.create_execution(
            session,
            execution_id="exec-deleted",
            graph_thread_id="email-ticket-exec-deleted",
            email_id=7,
        )


def test_production_module_exposes_one_authoritative_active_graph() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "workflows"
        / "email_ticket"
        / "active_graph.py"
    )
    source = module_path.read_text(encoding="utf-8")

    assert source.count("def build_active_") == 1
    assert "def build_active_email_ticket_graph" in source
    assert "build_active_validation_sap_graph" not in source


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["completed", "waiting_human", "waiting_external", "resume_queued"])
async def test_graph_start_verifies_stable_execution_without_replaying_graph(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    interrupt = SimpleNamespace(id="interrupt-1", value={"task_id": 7})
    interrupts = () if status == "completed" else (interrupt,)
    execution = SimpleNamespace(
        execution_id="exec-stable",
        graph_thread_id="email-ticket-exec-stable",
        email_id=7,
        ticket_id=9,
        status=status,
        checkpoint_id="checkpoint-stable",
        checkpoint_step=12,
        last_error_code=None,
        completed_at=None,
    )
    ledger = None if status == "completed" else SimpleNamespace(
        status="resume_queued" if status == "resume_queued" else "pending",
        checkpoint_id="checkpoint-stable",
        checkpoint_step=12,
    )
    graph = SimpleNamespace(
        aget_state=AsyncMock(
            return_value=_snapshot(
                execution_id="exec-stable",
                checkpoint_id="checkpoint-stable",
                checkpoint_step=12,
                execution_state="completed" if status == "completed" else "waiting_human_review",
                interrupts=interrupts,
            )
        ),
        ainvoke=AsyncMock(),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[execution, ledger]),
        commit=AsyncMock(),
    )
    create = AsyncMock(return_value=execution)
    monkeypatch.setattr(runner, "create_execution", create)
    monkeypatch.setattr(runner, "build_active_email_ticket_graph", lambda **_kwargs: graph)
    record = AsyncMock()
    monkeypatch.setattr(runner, "record_graph_result", record)

    result = await runner.run_active_email_ticket_workflow(
        session,
        checkpointer=object(),
        email_id=7,
        execution_id="exec-stable",
        trigger_job_id=21,
    )

    assert result["execution_id"] == "exec-stable"
    graph.ainvoke.assert_not_awaited()
    record.assert_not_awaited()
    assert execution.status == status
    assert create.await_args.kwargs["trigger_job_id"] == 21


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("persisted_id", "persisted_step", "current_id", "current_step", "error"),
    [
        (None, None, None, None, "WORKFLOW_CHECKPOINT_MISSING"),
        ("checkpoint-12", 12, "checkpoint-11", 11, "WORKFLOW_CHECKPOINT_REGRESSION"),
        ("checkpoint-12", 12, "checkpoint-13", 13, "WORKFLOW_CHECKPOINT_ADVANCED_UNRECORDED"),
        ("checkpoint-12", 12, "fork-12", 12, "WORKFLOW_CHECKPOINT_DIVERGED"),
        ("checkpoint-12", 12, "checkpoint-12", None, "WORKFLOW_CHECKPOINT_ORDER_UNVERIFIABLE"),
    ],
)
async def test_graph_start_fails_closed_for_unverified_stable_execution(
    monkeypatch: pytest.MonkeyPatch,
    persisted_id: str | None,
    persisted_step: int | None,
    current_id: str | None,
    current_step: int | None,
    error: str,
) -> None:
    execution = SimpleNamespace(
        execution_id="exec-stable",
        graph_thread_id="email-ticket-exec-stable",
        email_id=7,
        status="completed",
        checkpoint_id=persisted_id,
        checkpoint_step=persisted_step,
        last_error_code=None,
        completed_at=object(),
    )
    graph = SimpleNamespace(
        aget_state=AsyncMock(
            return_value=_snapshot(
                execution_id="exec-stable",
                checkpoint_id=current_id,
                checkpoint_step=current_step,
            )
        ),
        ainvoke=AsyncMock(),
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=execution), commit=AsyncMock())
    monkeypatch.setattr(runner, "create_execution", AsyncMock(return_value=execution))
    monkeypatch.setattr(runner, "build_active_email_ticket_graph", lambda **_kwargs: graph)

    with pytest.raises(RuntimeError, match=error):
        await runner.run_active_email_ticket_workflow(
            session,
            checkpointer=object(),
            email_id=7,
            execution_id="exec-stable",
        )

    graph.ainvoke.assert_not_awaited()
    assert execution.status == "completed"
    assert execution.completed_at is not None


@pytest.mark.anyio
async def test_failed_graph_start_recovery_continues_same_checkpoint(monkeypatch) -> None:
    execution = SimpleNamespace(
        execution_id="exec-failed",
        graph_thread_id="email-ticket-exec-failed",
        email_id=7,
        status="failed",
        last_error_code="WORKFLOW_TRANSIENT_FAILURE",
        completed_at=object(),
    )
    before = _snapshot(
        execution_id="exec-failed",
        checkpoint_id="checkpoint-before",
        checkpoint_step=8,
        execution_state="sap_submit_completed",
        next_nodes=("reconcile_sap",),
    )
    after = _snapshot(
        execution_id="exec-failed",
        checkpoint_id="checkpoint-after",
        checkpoint_step=9,
        execution_state="completed",
    )
    result = {"execution_id": "exec-failed", "execution_state": "completed"}
    graph = SimpleNamespace(
        aget_state=AsyncMock(side_effect=[before, after]),
        ainvoke=AsyncMock(return_value=result),
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=execution), commit=AsyncMock())
    create = AsyncMock(return_value=execution)
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(runner, "create_execution", create)
    monkeypatch.setattr(runner, "build_active_email_ticket_graph", lambda **_kwargs: graph)
    monkeypatch.setattr(runner, "record_graph_result", record)

    recovered = await runner.run_active_email_ticket_workflow(
        session,
        checkpointer=object(),
        email_id=7,
        execution_id="exec-failed",
        trigger_job_id=21,
    )

    assert recovered["execution_id"] == "exec-failed"
    graph.ainvoke.assert_awaited_once()
    assert graph.ainvoke.await_args.args[0] is None
    assert create.await_args.kwargs["trigger_job_id"] == 21
    record.assert_awaited_once_with(
        session,
        execution=execution,
        result=result,
        checkpoint_id="checkpoint-after",
        checkpoint_step=9,
    )


@pytest.mark.anyio
async def test_failed_graph_start_recovery_never_restarts_without_checkpoint(monkeypatch) -> None:
    execution = SimpleNamespace(
        execution_id="exec-failed",
        graph_thread_id="email-ticket-exec-failed",
        email_id=7,
        status="failed",
        last_error_code="WORKFLOW_CHECKPOINT_MISSING",
        completed_at=object(),
    )
    empty = SimpleNamespace(config=None, metadata={}, values={}, tasks=(), next=())
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=empty), ainvoke=AsyncMock())
    session = SimpleNamespace(scalar=AsyncMock(return_value=execution), commit=AsyncMock())
    monkeypatch.setattr(runner, "create_execution", AsyncMock(return_value=execution))
    monkeypatch.setattr(runner, "build_active_email_ticket_graph", lambda **_kwargs: graph)

    with pytest.raises(RuntimeError, match="WORKFLOW_CHECKPOINT_MISSING"):
        await runner.run_active_email_ticket_workflow(
            session,
            checkpointer=object(),
            email_id=7,
            execution_id="exec-failed",
            trigger_job_id=21,
        )

    graph.ainvoke.assert_not_awaited()
    assert execution.status == "failed"
    assert execution.last_error_code == "WORKFLOW_CHECKPOINT_MISSING"


def test_execution_checkpoint_identity_advances_monotonically() -> None:
    execution = SimpleNamespace(checkpoint_id=None, checkpoint_step=None)
    executions._record_execution_checkpoint(
        execution,
        checkpoint_id="checkpoint-10",
        checkpoint_step=10,
    )
    executions._record_execution_checkpoint(
        execution,
        checkpoint_id="checkpoint-11",
        checkpoint_step=11,
    )
    assert (execution.checkpoint_id, execution.checkpoint_step) == ("checkpoint-11", 11)

    for checkpoint_id, checkpoint_step, error in (
        ("checkpoint-10", 10, "WORKFLOW_CHECKPOINT_REGRESSION"),
        ("fork-11", 11, "WORKFLOW_CHECKPOINT_DIVERGED"),
        ("checkpoint-11", 12, "WORKFLOW_CHECKPOINT_IDENTITY_REUSED"),
    ):
        with pytest.raises(RuntimeError, match=error):
            executions._record_execution_checkpoint(
                execution,
                checkpoint_id=checkpoint_id,
                checkpoint_step=checkpoint_step,
            )


@pytest.mark.parametrize(
    ("status", "interrupts", "next_nodes", "error"),
    [
        ("completed", (SimpleNamespace(id="unexpected", value={}),), (), "WORKFLOW_CHECKPOINT_STATE_CONFLICT"),
        ("completed", (), ("send_reply",), "WORKFLOW_CHECKPOINT_STATE_CONFLICT"),
        ("waiting_human", (), (), "WORKFLOW_CHECKPOINT_INTERRUPT_MISSING"),
        ("waiting_external", (), (), "WORKFLOW_CHECKPOINT_INTERRUPT_MISSING"),
        ("resume_queued", (), (), "WORKFLOW_CHECKPOINT_INTERRUPT_MISSING"),
    ],
)
def test_stable_execution_rejects_checkpoint_shape_conflicts(
    status: str,
    interrupts: tuple,
    next_nodes: tuple,
    error: str,
) -> None:
    execution = SimpleNamespace(
        execution_id="exec-shape",
        status=status,
        checkpoint_id="checkpoint-9",
        checkpoint_step=9,
    )
    snapshot = _snapshot(
        execution_id="exec-shape",
        checkpoint_id="checkpoint-9",
        checkpoint_step=9,
        interrupts=interrupts,
        next_nodes=next_nodes,
    )
    with pytest.raises(RuntimeError, match=error):
        runner._stable_execution_result(execution, snapshot)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("execution_status", "ledger", "error"),
    [
        ("waiting_human", None, "WORKFLOW_INTERRUPT_LEDGER_MISSING"),
        (
            "waiting_human",
            SimpleNamespace(status="resumed", checkpoint_id="checkpoint-9", checkpoint_step=9),
            "WORKFLOW_INTERRUPT_LEDGER_STATUS_CONFLICT",
        ),
        (
            "resume_queued",
            SimpleNamespace(status="pending", checkpoint_id="checkpoint-9", checkpoint_step=9),
            "WORKFLOW_INTERRUPT_LEDGER_STATUS_CONFLICT",
        ),
        (
            "waiting_external",
            SimpleNamespace(status="pending", checkpoint_id="fork-9", checkpoint_step=9),
            "WORKFLOW_INTERRUPT_LEDGER_CHECKPOINT_CONFLICT",
        ),
    ],
)
async def test_waiting_execution_verifies_interrupt_ledger(
    execution_status: str,
    ledger,
    error: str,
) -> None:
    execution = SimpleNamespace(
        execution_id="exec-ledger",
        status=execution_status,
        checkpoint_id="checkpoint-9",
        checkpoint_step=9,
    )
    snapshot = _snapshot(
        execution_id="exec-ledger",
        checkpoint_id="checkpoint-9",
        checkpoint_step=9,
        execution_state="waiting_human_review",
        interrupts=(SimpleNamespace(id="interrupt-9", value={"task_id": 7}),),
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=ledger))

    with pytest.raises(RuntimeError, match=error):
        await runner._verified_stable_execution_result(session, execution, snapshot)


@pytest.mark.anyio
async def test_completed_execution_rejects_active_interrupt_ledger() -> None:
    execution = SimpleNamespace(
        execution_id="exec-completed-ledger",
        status="completed",
        checkpoint_id="checkpoint-9",
        checkpoint_step=9,
    )
    snapshot = _snapshot(
        execution_id="exec-completed-ledger",
        checkpoint_id="checkpoint-9",
        checkpoint_step=9,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(status="pending")))

    with pytest.raises(RuntimeError, match="WORKFLOW_INTERRUPT_LEDGER_CONFLICT"):
        await runner._verified_stable_execution_result(session, execution, snapshot)


@pytest.mark.anyio
async def test_duplicate_resume_of_completed_execution_verifies_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = SimpleNamespace(
        execution_id="exec-completed",
        graph_thread_id="thread-completed",
        status="completed",
        checkpoint_id="checkpoint-20",
        checkpoint_step=20,
        last_error_code="WORKFLOW_PREVIOUS_TRANSIENT_ERROR",
    )
    graph = SimpleNamespace(
        aget_state=AsyncMock(
            return_value=_snapshot(
                execution_id="exec-completed",
                checkpoint_id="checkpoint-20",
                checkpoint_step=20,
            )
        ),
        ainvoke=AsyncMock(),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[execution, None]),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(runner, "get_interrupt_for_resume", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "build_active_email_ticket_graph", lambda **_kwargs: graph)

    result = await runner.resume_active_email_ticket_workflow(
        session,
        checkpointer=object(),
        execution_id="exec-completed",
        interrupt_id="already-resumed",
        response_payload={"action": "duplicate"},
    )

    assert result["execution_state"] == "completed"
    graph.ainvoke.assert_not_awaited()
    assert execution.last_error_code is None
    session.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_duplicate_resume_of_completed_execution_rejects_missing_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = SimpleNamespace(
        execution_id="exec-completed",
        graph_thread_id="thread-completed",
        status="completed",
        checkpoint_id="checkpoint-20",
        checkpoint_step=20,
        last_error_code=None,
    )
    graph = SimpleNamespace(
        aget_state=AsyncMock(
            return_value=_snapshot(
                execution_id="exec-completed",
                checkpoint_id=None,
                checkpoint_step=None,
            )
        ),
        ainvoke=AsyncMock(),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=execution),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(runner, "get_interrupt_for_resume", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "build_active_email_ticket_graph", lambda **_kwargs: graph)

    with pytest.raises(RuntimeError, match="WORKFLOW_CHECKPOINT_MISSING"):
        await runner.resume_active_email_ticket_workflow(
            session,
            checkpointer=object(),
            execution_id="exec-completed",
            interrupt_id="already-resumed",
            response_payload={"action": "duplicate"},
        )

    graph.ainvoke.assert_not_awaited()
    assert execution.last_error_code == "WORKFLOW_CHECKPOINT_MISSING"
    session.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_resume_reconciles_advanced_checkpoint_without_reinjecting_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = SimpleNamespace(
        execution_id="exec-advanced",
        graph_thread_id="thread-advanced",
        status="resume_queued",
        checkpoint_id="checkpoint-before",
        checkpoint_step=10,
        completed_at=None,
    )
    ledger = SimpleNamespace(
        checkpoint_id="checkpoint-before",
        checkpoint_step=10,
        status="resume_queued",
        response_payload=None,
        resumed_by_user_id=None,
        resumed_at=None,
    )
    snapshot = SimpleNamespace(
        config={"configurable": {"checkpoint_id": "checkpoint-after"}},
        metadata={"step": 11},
        values={"execution_id": "exec-advanced", "execution_state": "completed"},
        tasks=(),
    )
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=snapshot), ainvoke=AsyncMock())
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(runner, "get_interrupt_for_resume", AsyncMock(return_value=(execution, ledger)))
    monkeypatch.setattr(runner, "build_active_email_ticket_graph", lambda **_kwargs: graph)
    monkeypatch.setattr(runner, "record_graph_result", record)

    result = await runner.resume_active_email_ticket_workflow(
        SimpleNamespace(),
        checkpointer=object(),
        execution_id="exec-advanced",
        interrupt_id="interrupt-1",
        response_payload={"task_id": 9, "action": "approve_send"},
        user_id=5,
    )

    graph.ainvoke.assert_not_awaited()
    assert result["execution_state"] == "completed"
    assert ledger.status == "resumed"
    record.assert_awaited_once()
    assert record.await_args.kwargs["checkpoint_id"] == "checkpoint-after"
    assert record.await_args.kwargs["checkpoint_step"] == 11


@pytest.mark.anyio
async def test_resume_injects_payload_when_checkpoint_has_not_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = SimpleNamespace(
        execution_id="exec-current",
        graph_thread_id="thread-current",
        status="resume_queued",
        checkpoint_id="checkpoint-before",
        checkpoint_step=10,
        completed_at=None,
    )
    ledger = SimpleNamespace(
        checkpoint_id="checkpoint-before",
        checkpoint_step=10,
        status="resume_queued",
        response_payload=None,
        resumed_by_user_id=None,
        resumed_at=None,
    )
    before = SimpleNamespace(
        config={"configurable": {"checkpoint_id": "checkpoint-before"}},
        metadata={"step": 10},
        values={"execution_id": "exec-current", "execution_state": "waiting_human_review"},
        tasks=(),
    )
    after = SimpleNamespace(
        config={"configurable": {"checkpoint_id": "checkpoint-after"}},
        metadata={"step": 11},
        values={"execution_id": "exec-current", "execution_state": "completed"},
        tasks=(),
    )
    graph = SimpleNamespace(
        aget_state=AsyncMock(side_effect=[before, after]),
        ainvoke=AsyncMock(
            return_value={"execution_id": "exec-current", "execution_state": "completed"}
        ),
    )
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(runner, "get_interrupt_for_resume", AsyncMock(return_value=(execution, ledger)))
    monkeypatch.setattr(runner, "build_active_email_ticket_graph", lambda **_kwargs: graph)
    monkeypatch.setattr(runner, "record_graph_result", record)
    monkeypatch.setattr(runner, "_active_runtime", lambda _session: object())

    payload = {"task_id": 9, "action": "approve_send"}
    await runner.resume_active_email_ticket_workflow(
        SimpleNamespace(),
        checkpointer=object(),
        execution_id="exec-current",
        interrupt_id="interrupt-1",
        response_payload=payload,
        user_id=5,
    )

    graph.ainvoke.assert_awaited_once()
    command = graph.ainvoke.await_args.args[0]
    assert command.resume == payload
    assert record.await_args.kwargs["checkpoint_id"] == "checkpoint-after"
    assert record.await_args.kwargs["checkpoint_step"] == 11


@pytest.mark.anyio
async def test_resume_fails_closed_when_persisted_checkpoint_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = SimpleNamespace(
        execution_id="exec-missing",
        graph_thread_id="thread-missing",
        status="resume_queued",
        checkpoint_id="checkpoint-before",
        checkpoint_step=10,
        completed_at=None,
    )
    ledger = SimpleNamespace(
        checkpoint_id="checkpoint-before",
        checkpoint_step=10,
        status="resume_queued",
        response_payload=None,
        resumed_by_user_id=None,
        resumed_at=None,
    )
    missing = SimpleNamespace(config=None, metadata=None, values={}, tasks=(), next=())
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=missing), ainvoke=AsyncMock())
    monkeypatch.setattr(runner, "get_interrupt_for_resume", AsyncMock(return_value=(execution, ledger)))
    monkeypatch.setattr(runner, "build_active_email_ticket_graph", lambda **_kwargs: graph)

    with pytest.raises(RuntimeError, match="WORKFLOW_CHECKPOINT_MISSING"):
        await runner.resume_active_email_ticket_workflow(
            SimpleNamespace(),
            checkpointer=object(),
            execution_id="exec-missing",
            interrupt_id="interrupt-1",
            response_payload={"task_id": 9, "action": "approve_send"},
            user_id=5,
        )

    graph.ainvoke.assert_not_awaited()
    assert ledger.status == "resume_queued"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("current_step", "expected_error"),
    [
        (None, "WORKFLOW_CHECKPOINT_ORDER_UNVERIFIABLE"),
        (9, "WORKFLOW_CHECKPOINT_REGRESSION"),
        (10, "WORKFLOW_CHECKPOINT_REGRESSION"),
    ],
)
async def test_resume_rejects_unverifiable_or_regressed_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    current_step: int | None,
    expected_error: str,
) -> None:
    execution = SimpleNamespace(
        execution_id="exec-regressed",
        graph_thread_id="thread-regressed",
        status="resume_queued",
        checkpoint_id="checkpoint-before",
        checkpoint_step=10,
        completed_at=None,
    )
    ledger = SimpleNamespace(
        checkpoint_id="checkpoint-before",
        checkpoint_step=10,
        status="resume_queued",
        response_payload=None,
        resumed_by_user_id=None,
        resumed_at=None,
    )
    snapshot = SimpleNamespace(
        config={"configurable": {"checkpoint_id": "different-checkpoint"}},
        metadata={} if current_step is None else {"step": current_step},
        values={"execution_id": "exec-regressed"},
        tasks=(),
    )
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=snapshot), ainvoke=AsyncMock())
    monkeypatch.setattr(runner, "get_interrupt_for_resume", AsyncMock(return_value=(execution, ledger)))
    monkeypatch.setattr(runner, "build_active_email_ticket_graph", lambda **_kwargs: graph)

    with pytest.raises(RuntimeError, match=expected_error):
        await runner.resume_active_email_ticket_workflow(
            SimpleNamespace(),
            checkpointer=object(),
            execution_id="exec-regressed",
            interrupt_id="interrupt-1",
            response_payload={"reason": "retry"},
        )

    graph.ainvoke.assert_not_awaited()
