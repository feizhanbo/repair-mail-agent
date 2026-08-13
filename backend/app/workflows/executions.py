from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ManualReviewTask, WorkflowExecution, WorkflowInterrupt
from app.services.common import utcnow


WORKFLOW_NAME = "email_ticket"
WORKFLOW_VERSION = "langgraph-v2"
STATE_SCHEMA_VERSION = "email-ticket-state-v1"


async def create_execution(
    session: AsyncSession,
    *,
    execution_id: str,
    graph_thread_id: str,
    email_id: int,
    trigger_job_id: int | None = None,
    mode: str = "langgraph",
) -> WorkflowExecution:
    existing = await session.scalar(
        select(WorkflowExecution).where(WorkflowExecution.execution_id == execution_id)
    )
    if existing is not None:
        if (
            existing.workflow_name != WORKFLOW_NAME
            or existing.graph_thread_id != graph_thread_id
            or existing.email_id != email_id
        ):
            raise ValueError("WORKFLOW_EXECUTION_IDENTITY_CONFLICT")
        existing_trigger_job_id = getattr(existing, "trigger_job_id", None)
        if trigger_job_id is not None:
            if existing_trigger_job_id not in {None, trigger_job_id}:
                raise ValueError("WORKFLOW_TRIGGER_JOB_IDENTITY_CONFLICT")
            if existing_trigger_job_id is None:
                existing.trigger_job_id = trigger_job_id
        if getattr(existing, "status", None) == "failed":
            existing.status = "running"
            existing.completed_at = None
        return existing
    execution = WorkflowExecution(
        execution_id=execution_id,
        graph_thread_id=graph_thread_id,
        workflow_name=WORKFLOW_NAME,
        workflow_version=WORKFLOW_VERSION,
        state_schema_version=STATE_SCHEMA_VERSION,
        execution_mode=mode,
        status="running",
        email_id=email_id,
        trigger_job_id=trigger_job_id,
        started_at=utcnow(),
    )
    session.add(execution)
    await session.flush()
    return execution


async def record_graph_result(
    session: AsyncSession,
    *,
    execution: WorkflowExecution,
    result: dict[str, Any],
    checkpoint_id: str | None = None,
    checkpoint_step: int | None = None,
) -> WorkflowInterrupt | None:
    _record_execution_checkpoint(
        execution,
        checkpoint_id=checkpoint_id,
        checkpoint_step=checkpoint_step,
    )
    execution.last_error_code = None
    execution.ticket_id = result.get("ticket_id") or execution.ticket_id
    execution.current_node = str(result.get("execution_state") or "unknown")[:100]
    route_history = list(result.get("route_history") or [])
    execution.last_route = str(route_history[-1])[:100] if route_history else None
    interrupts = list(result.get("__interrupt__") or [])
    execution.result_summary = {
        "outcome": result.get("workflow_outcome") or result.get("shadow_outcome"),
        "execution_state": result.get("execution_state"),
        "route_count": len(route_history),
    }
    if not interrupts:
        execution.status = "completed"
        execution.completed_at = utcnow()
        return None

    current = interrupts[0]
    interrupt_id = str(current.id)
    payload = dict(current.value) if isinstance(current.value, dict) else {"value": current.value}
    ledger = await session.scalar(
        select(WorkflowInterrupt).where(
            WorkflowInterrupt.execution_id == execution.execution_id,
            WorkflowInterrupt.interrupt_id == interrupt_id,
        )
    )
    if ledger is not None and ledger.status not in {"pending", "resume_queued"}:
        suffix = 2
        base_interrupt_id = interrupt_id
        while await session.scalar(
            select(WorkflowInterrupt.id).where(
                WorkflowInterrupt.execution_id == execution.execution_id,
                WorkflowInterrupt.interrupt_id == f"{base_interrupt_id}:{suffix}",
            )
        ):
            suffix += 1
        interrupt_id = f"{base_interrupt_id}:{suffix}"
        ledger = None
    if ledger is None:
        ledger = WorkflowInterrupt(
            execution_id=execution.execution_id,
            interrupt_id=interrupt_id,
            checkpoint_id=checkpoint_id,
            checkpoint_step=checkpoint_step,
            manual_task_id=payload.get("task_id"),
            status="pending",
            request_payload=payload,
            expected_ticket_version=payload.get("expected_ticket_version"),
        )
        session.add(ledger)
    elif checkpoint_id and not ledger.checkpoint_id:
        ledger.checkpoint_id = checkpoint_id
        ledger.checkpoint_step = checkpoint_step
    elif checkpoint_id == ledger.checkpoint_id and ledger.checkpoint_step is None:
        ledger.checkpoint_step = checkpoint_step
    execution.status = "waiting_human" if payload.get("task_id") else "waiting_external"
    await session.flush()
    return ledger


def _record_execution_checkpoint(
    execution: WorkflowExecution,
    *,
    checkpoint_id: str | None,
    checkpoint_step: int | None,
) -> None:
    """Persist a monotonic cross-store identity for every recorded Graph result."""
    if checkpoint_id is None or checkpoint_step is None:
        raise RuntimeError("WORKFLOW_CHECKPOINT_IDENTITY_MISSING")
    previous_id = execution.checkpoint_id
    previous_step = execution.checkpoint_step
    if previous_id is not None:
        if previous_step is None:
            raise RuntimeError("WORKFLOW_CHECKPOINT_ORDER_UNVERIFIABLE")
        if checkpoint_step < previous_step:
            raise RuntimeError("WORKFLOW_CHECKPOINT_REGRESSION")
        if checkpoint_step == previous_step and checkpoint_id != previous_id:
            raise RuntimeError("WORKFLOW_CHECKPOINT_DIVERGED")
        if checkpoint_step > previous_step and checkpoint_id == previous_id:
            raise RuntimeError("WORKFLOW_CHECKPOINT_IDENTITY_REUSED")
    execution.checkpoint_id = checkpoint_id
    execution.checkpoint_step = checkpoint_step


async def get_pending_task_interrupt(
    session: AsyncSession,
    *,
    manual_task_id: int,
) -> tuple[WorkflowExecution, WorkflowInterrupt] | None:
    statement = (
        select(WorkflowExecution, WorkflowInterrupt)
        .join(WorkflowInterrupt, WorkflowInterrupt.execution_id == WorkflowExecution.execution_id)
        .where(
            WorkflowInterrupt.manual_task_id == manual_task_id,
            WorkflowInterrupt.status.in_({"pending", "resume_queued"}),
        )
        .order_by(WorkflowInterrupt.id.desc())
        .with_for_update()
    )
    return (await session.execute(statement)).first()


def ensure_interrupt_action_allowed(
    workflow_interrupt: WorkflowInterrupt,
    *,
    action: str,
) -> None:
    allowed = (workflow_interrupt.request_payload or {}).get("allowed_actions")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("WORKFLOW_INTERRUPT_ALLOWED_ACTIONS_MISSING")
    if action not in {str(item) for item in allowed}:
        raise ValueError("WORKFLOW_INTERRUPT_ACTION_NOT_ALLOWED")


def mark_interrupt_resumed(
    execution: WorkflowExecution,
    workflow_interrupt: WorkflowInterrupt,
    *,
    response_payload: dict[str, Any],
    user_id: int | None,
) -> None:
    workflow_interrupt.status = "resumed"
    workflow_interrupt.response_payload = response_payload
    workflow_interrupt.resumed_by_user_id = user_id
    workflow_interrupt.resumed_at = utcnow()
    execution.status = "running"
    execution.completed_at = None


async def enqueue_manual_resume_if_bound(
    session: AsyncSession,
    *,
    manual_task_id: int,
    action: str,
    edited_fields: dict[str, Any] | None,
    reviewer_id: int,
    expected_ticket_version: int | None,
    next_action: str | None = None,
) -> dict[str, Any] | None:
    pair = await get_pending_task_interrupt(session, manual_task_id=manual_task_id)
    if pair is None:
        return None
    execution, workflow_interrupt = pair
    if workflow_interrupt.status != "pending":
        raise ValueError("WORKFLOW_INTERRUPT_RESUME_ALREADY_QUEUED")
    ensure_interrupt_action_allowed(workflow_interrupt, action=action)
    from app.services.jobs import enqueue_job

    response_payload = {
        "task_id": manual_task_id,
        "action": action,
        "edited_fields": edited_fields or {},
        "reviewer_id": reviewer_id,
        "expected_ticket_version": workflow_interrupt.expected_ticket_version,
        "next_action": next_action,
    }
    job = await enqueue_job(
        session,
        job_type="graph_resume",
        resource_type="workflow_execution",
        resource_id=execution.id,
        idempotency_key=f"graph_resume:{execution.execution_id}:{workflow_interrupt.interrupt_id}",
        metadata={
            "execution_id": execution.execution_id,
            "interrupt_id": workflow_interrupt.interrupt_id,
            "response_payload": response_payload,
            "user_id": reviewer_id,
        },
        max_attempts=3,
        reactivate_terminal=True,
    )
    workflow_interrupt.status = "resume_queued"
    workflow_interrupt.response_payload = response_payload
    execution.status = "resume_queued"
    return {"execution_id": execution.execution_id, "resume_job_id": job.id}


async def enqueue_reply_resume_if_bound(
    session: AsyncSession,
    *,
    reply_id: int,
    reviewer_id: int,
    action: str = "approve_send",
) -> dict[str, Any] | None:
    if action not in {"approve_send", "reject_send"}:
        raise ValueError("WORKFLOW_REPLY_ACTION_NOT_SUPPORTED")
    rows = (
        await session.execute(
            select(WorkflowExecution, WorkflowInterrupt)
            .join(WorkflowInterrupt, WorkflowInterrupt.execution_id == WorkflowExecution.execution_id)
            .where(WorkflowInterrupt.status.in_({"pending", "resume_queued"}))
            .order_by(WorkflowInterrupt.id.desc())
            .with_for_update()
        )
    ).all()
    pair = next(
        (
            (execution, workflow_interrupt)
            for execution, workflow_interrupt in rows
            if int((workflow_interrupt.request_payload or {}).get("reply_id") or 0) == reply_id
        ),
        None,
    )
    if pair is None:
        return None
    execution, workflow_interrupt = pair
    ensure_interrupt_action_allowed(workflow_interrupt, action=action)
    from app.services.jobs import enqueue_job

    task_id = workflow_interrupt.manual_task_id
    response_payload = {
        "task_id": task_id,
        "action": action,
        "edited_fields": {},
        "reviewer_id": reviewer_id,
        "expected_ticket_version": workflow_interrupt.expected_ticket_version,
    }
    job = await enqueue_job(
        session,
        job_type="graph_resume",
        resource_type="workflow_execution",
        resource_id=execution.id,
        idempotency_key=f"graph_resume:{execution.execution_id}:{workflow_interrupt.interrupt_id}",
        metadata={
            "execution_id": execution.execution_id,
            "interrupt_id": workflow_interrupt.interrupt_id,
            "response_payload": response_payload,
            "user_id": reviewer_id,
        },
        max_attempts=3,
        reactivate_terminal=True,
    )
    workflow_interrupt.status = "resume_queued"
    workflow_interrupt.response_payload = response_payload
    execution.status = "resume_queued"
    if task_id is not None:
        task = await session.get(ManualReviewTask, task_id, with_for_update=True)
        if task is not None and task.status not in {"resolved", "closed"}:
            task.status = "resolved"
            task.resolved_by_user_id = reviewer_id
            task.resolved_at = utcnow()
            task.resolution = (
                "Reply approved; bound LangGraph execution queued for SMTP resume."
                if action == "approve_send"
                else "Reply rejected; bound LangGraph execution queued for terminal resume."
            )
    return {"execution_id": execution.execution_id, "resume_job_id": job.id}


async def reply_has_pending_interrupt(
    session: AsyncSession,
    *,
    reply_id: int,
) -> bool:
    """Treat pending and queued-resume ledgers as active Graph ownership."""
    payload_reply_id = str(reply_id)
    rows = (
        await session.execute(
            select(WorkflowInterrupt.request_payload)
            .where(WorkflowInterrupt.status.in_({"pending", "resume_queued"}))
            .order_by(WorkflowInterrupt.id.desc())
        )
    ).scalars().all()
    return any(str((payload or {}).get("reply_id") or "") == payload_reply_id for payload in rows)


async def retry_pending_external_interrupt(
    session: AsyncSession,
    *,
    execution_id: str,
    interrupt_id: str,
    operator_user_id: int,
) -> dict[str, Any]:
    """Explicitly reactivate one failed scheduler-owned resume job."""
    pair = (
        await session.execute(
            select(WorkflowExecution, WorkflowInterrupt)
            .join(WorkflowInterrupt, WorkflowInterrupt.execution_id == WorkflowExecution.execution_id)
            .where(
                WorkflowExecution.execution_id == execution_id,
                WorkflowInterrupt.interrupt_id == interrupt_id,
                WorkflowInterrupt.status == "pending",
            )
            .with_for_update()
        )
    ).first()
    if pair is None:
        raise LookupError("WORKFLOW_EXTERNAL_INTERRUPT_NOT_PENDING")
    execution, workflow_interrupt = pair
    if workflow_interrupt.manual_task_id is not None:
        raise ValueError("WORKFLOW_HUMAN_INTERRUPT_REQUIRES_TASK_RESUBMISSION")
    from app.services.jobs import enqueue_job

    response_payload = dict(workflow_interrupt.response_payload or {})
    response_payload.setdefault("reason", "operator_retry")
    job = await enqueue_job(
        session,
        job_type="graph_resume",
        resource_type="workflow_execution",
        resource_id=execution.id,
        idempotency_key=f"graph_resume:{execution.execution_id}:{workflow_interrupt.interrupt_id}",
        metadata={
            "execution_id": execution.execution_id,
            "interrupt_id": workflow_interrupt.interrupt_id,
            "response_payload": response_payload,
            "user_id": operator_user_id,
        },
        max_attempts=3,
        reactivate_terminal=True,
    )
    workflow_interrupt.status = "resume_queued"
    workflow_interrupt.response_payload = response_payload
    workflow_interrupt.error_message = None
    execution.status = "resume_queued"
    execution.last_error_code = None
    return {
        "execution_id": execution.execution_id,
        "interrupt_id": workflow_interrupt.interrupt_id,
        "resume_job_id": job.id,
        "status": "resume_queued",
    }


async def get_interrupt_for_resume(
    session: AsyncSession,
    *,
    execution_id: str,
    interrupt_id: str,
) -> tuple[WorkflowExecution, WorkflowInterrupt] | None:
    statement = (
        select(WorkflowExecution, WorkflowInterrupt)
        .join(WorkflowInterrupt, WorkflowInterrupt.execution_id == WorkflowExecution.execution_id)
        .where(
            WorkflowExecution.execution_id == execution_id,
            WorkflowInterrupt.interrupt_id == interrupt_id,
            WorkflowInterrupt.status.in_({"pending", "resume_queued"}),
        )
        .with_for_update()
    )
    return (await session.execute(statement)).first()
