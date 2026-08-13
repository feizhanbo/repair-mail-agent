from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.audit import log_system_event
from app.models import WorkflowExecution, WorkflowInterrupt
from app.services.jobs import enqueue_job
from app.services.common import utcnow
from app.workflows.email_ticket.active_graph import build_active_email_ticket_graph
from app.workflows.email_ticket.adapters import ActiveWorkflowServices, ReadOnlyEmailSnapshotLoader
from app.workflows.executions import create_execution, get_interrupt_for_resume, mark_interrupt_resumed, record_graph_result
from app.workflows.email_ticket.graph import build_shadow_email_ticket_graph
from app.workflows.email_ticket.state import EmailTicketRuntime, EmailTicketState


_CHECKPOINT_STABLE_EXECUTION_STATUSES = {
    "waiting_human",
    "waiting_external",
    "resume_queued",
    "completed",
}


async def run_shadow_email_ticket_workflow(
    session: AsyncSession,
    *,
    email_id: int,
    execution_id: str | None = None,
) -> EmailTicketState:
    """Run graph orchestration against persisted facts without business writes."""
    resolved_execution_id = execution_id or uuid4().hex
    result = await build_shadow_email_ticket_graph().ainvoke(
        {
            "execution_id": resolved_execution_id,
            "graph_thread_id": f"shadow-email-{email_id}",
            "email_id": email_id,
            "execution_state": "created",
            "route_history": [],
        },
        context=EmailTicketRuntime(
            load_email=ReadOnlyEmailSnapshotLoader(session),
            auto_apply_min_confidence=settings.AUTO_APPLY_MIN_CONFIDENCE,
        ),
    )
    return EmailTicketState(**result)


async def run_and_record_shadow_comparison(
    session: AsyncSession,
    *,
    email_id: int,
    legacy_outcome: dict | None = None,
) -> EmailTicketState:
    """Persist a sanitized comparison event without changing ticket business facts."""
    result = await run_shadow_email_ticket_workflow(session, email_id=email_id)
    legacy_summary = _legacy_summary(legacy_outcome or {})
    shadow_summary = {
        "outcome": result.get("workflow_outcome") or result.get("shadow_outcome"),
        "intent_type": result.get("ai_result", {}).get("intent_type"),
        "ticket_id": result.get("ticket_id"),
        "validation_outcome": result.get("validation_plan", {}).get("outcome"),
    }
    await log_system_event(
        session,
        event_type="langgraph_shadow_comparison",
        module_name="email_ticket_workflow",
        message="Read-only LangGraph result compared with legacy workflow outcome",
        correlation_id=result.get("execution_id"),
        email_id=email_id,
        ticket_id=result.get("ticket_id"),
        event_stage="shadow_comparison",
        event_status="matched" if legacy_summary == shadow_summary else "different",
        details={
            "legacy": legacy_summary,
            "shadow": shadow_summary,
            "route_history": result.get("route_history", []),
        },
    )
    return result


def _legacy_summary(value: dict) -> dict:
    parse = value.get("parse") if isinstance(value.get("parse"), dict) else value
    ticket = parse.get("ticket") if isinstance(parse.get("ticket"), dict) else {}
    return {
        "outcome": parse.get("shadow_outcome") or parse.get("status"),
        "intent_type": parse.get("intent_type"),
        "ticket_id": ticket.get("id") or parse.get("ticket_id"),
        "validation_outcome": (
            parse.get("export_validation", {}).get("status")
            if isinstance(parse.get("export_validation"), dict)
            else None
        ),
    }


def _active_runtime(session: AsyncSession) -> EmailTicketRuntime:
    services = ActiveWorkflowServices(session)
    return EmailTicketRuntime(
        load_email=ReadOnlyEmailSnapshotLoader(session),
        create_human_task=services.create_human_task,
        validate_ticket=services.validate_ticket,
        submit_sap=services.submit_sap,
        reconcile_sap=services.reconcile_sap,
        poll_sap=services.poll_sap,
        prepare_rma=services.prepare_rma,
        send_rma=services.send_rma,
        finalize_rma_archive=services.finalize_rma_archive,
        prepare_reply=services.prepare_reply,
        send_reply=services.send_reply,
        prepare_email_parse=services.prepare_email_parse,
        generate_ai_candidate=services.generate_ai_candidate,
        adopt_email_candidate=services.adopt_email_candidate,
        apply_human_decision=services.apply_human_decision,
        record_node_event=services.record_node_event,
        auto_apply_min_confidence=settings.AUTO_APPLY_MIN_CONFIDENCE,
    )


async def run_active_email_ticket_workflow(
    session: AsyncSession,
    *,
    checkpointer,
    email_id: int,
    execution_id: str | None = None,
    trigger_job_id: int | None = None,
    parse_request: dict | None = None,
) -> EmailTicketState:
    resolved_execution_id = execution_id or uuid4().hex
    graph_thread_id = f"email-ticket-{resolved_execution_id}"
    existing_execution = await session.scalar(
        select(WorkflowExecution).where(WorkflowExecution.execution_id == resolved_execution_id)
    )
    existing_execution_was_failed = bool(
        existing_execution is not None and existing_execution.status == "failed"
    )
    execution = await create_execution(
        session,
        execution_id=resolved_execution_id,
        graph_thread_id=graph_thread_id,
        email_id=email_id,
        trigger_job_id=trigger_job_id,
    )
    # The business-side execution identity must survive independently of the
    # PostgreSQL checkpoint write that follows.
    await session.commit()
    graph = build_active_email_ticket_graph(checkpointer=checkpointer)
    try:
        config = {"configurable": {"thread_id": graph_thread_id}}
        if existing_execution is not None:
            snapshot = await graph.aget_state(config)
            if execution.status in _CHECKPOINT_STABLE_EXECUTION_STATUSES:
                result = await _verified_stable_execution_result(session, execution, snapshot)
                if execution.last_error_code is not None:
                    execution.last_error_code = None
                    await session.commit()
                return EmailTicketState(**result)
            checkpoint_interrupts = _snapshot_interrupts(snapshot)
            if checkpoint_interrupts:
                result = dict(snapshot.values)
                result["__interrupt__"] = checkpoint_interrupts
            elif snapshot.next:
                result = await graph.ainvoke(None, config, context=_active_runtime(session))
            elif snapshot.values:
                result = dict(snapshot.values)
            elif existing_execution_was_failed:
                raise RuntimeError("WORKFLOW_CHECKPOINT_MISSING")
            else:
                result = await _invoke_new_active_graph(
                    graph,
                    config=config,
                    context=_active_runtime(session),
                    execution_id=resolved_execution_id,
                    graph_thread_id=graph_thread_id,
                    email_id=email_id,
                    parse_request=parse_request,
                )
        else:
            result = await _invoke_new_active_graph(
                graph,
                config=config,
                context=_active_runtime(session),
                execution_id=resolved_execution_id,
                graph_thread_id=graph_thread_id,
                email_id=email_id,
                parse_request=parse_request,
            )
    except Exception as exc:
        execution.last_error_code = _workflow_error_code(exc)
        if execution.status == "running":
            execution.status = "failed"
            execution.completed_at = utcnow()
        await session.commit()
        raise
    snapshot = await graph.aget_state(config)
    workflow_interrupt = await record_graph_result(
        session,
        execution=execution,
        result=result,
        checkpoint_id=_snapshot_checkpoint_id(snapshot),
        checkpoint_step=_snapshot_checkpoint_step(snapshot),
    )
    if workflow_interrupt is not None and workflow_interrupt.manual_task_id is None:
        await _enqueue_resume_job(session, execution=execution, workflow_interrupt=workflow_interrupt)
    return EmailTicketState(**result)


async def _invoke_new_active_graph(
    graph,
    *,
    config: dict,
    context: EmailTicketRuntime,
    execution_id: str,
    graph_thread_id: str,
    email_id: int,
    parse_request: dict | None,
) -> dict:
    return await graph.ainvoke(
        {
            "execution_id": execution_id,
            "graph_thread_id": graph_thread_id,
            "email_id": email_id,
            "execution_state": "created",
            "route_history": [],
            "parse_request": parse_request or {},
        },
        config,
        context=context,
    )


async def resume_active_email_ticket_workflow(
    session: AsyncSession,
    *,
    checkpointer,
    execution_id: str,
    interrupt_id: str,
    response_payload: dict,
    user_id: int | None = None,
) -> EmailTicketState:
    pair = await get_interrupt_for_resume(
        session,
        execution_id=execution_id,
        interrupt_id=interrupt_id,
    )
    if pair is None:
        execution = await session.scalar(
            select(WorkflowExecution).where(WorkflowExecution.execution_id == execution_id)
        )
        if execution is not None and execution.status == "completed":
            graph = build_active_email_ticket_graph(checkpointer=checkpointer)
            snapshot = await graph.aget_state(
                {"configurable": {"thread_id": execution.graph_thread_id}}
            )
            try:
                result = await _verified_stable_execution_result(session, execution, snapshot)
            except Exception as exc:
                execution.last_error_code = _workflow_error_code(exc)
                await session.commit()
                raise
            if execution.last_error_code is not None:
                execution.last_error_code = None
                await session.commit()
            return EmailTicketState(**result)
        raise LookupError("WORKFLOW_INTERRUPT_NOT_RESUMABLE")
    execution, workflow_interrupt = pair
    graph = build_active_email_ticket_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": execution.graph_thread_id}}
    before = await graph.aget_state(config)
    current_checkpoint_id = _snapshot_checkpoint_id(before)
    current_checkpoint_step = _snapshot_checkpoint_step(before)
    if workflow_interrupt.checkpoint_id and current_checkpoint_id is None:
        raise RuntimeError("WORKFLOW_CHECKPOINT_MISSING")
    if (
        workflow_interrupt.checkpoint_id
        and current_checkpoint_id
        and current_checkpoint_id != workflow_interrupt.checkpoint_id
    ):
        ledger_step = getattr(workflow_interrupt, "checkpoint_step", None)
        if ledger_step is None or current_checkpoint_step is None:
            raise RuntimeError("WORKFLOW_CHECKPOINT_ORDER_UNVERIFIABLE")
        if current_checkpoint_step <= ledger_step:
            raise RuntimeError("WORKFLOW_CHECKPOINT_REGRESSION")
        # PostgreSQL has advanced but the MySQL ledger commit was lost.  Sync
        # durable facts without injecting the stale resume payload again.
        result = dict(before.values)
        checkpoint_interrupts = [item for task in before.tasks for item in task.interrupts]
        if checkpoint_interrupts:
            result["__interrupt__"] = checkpoint_interrupts
    else:
        result = await graph.ainvoke(
            Command(resume=response_payload),
            config,
            context=_active_runtime(session),
        )
        before = await graph.aget_state(config)
        current_checkpoint_id = _snapshot_checkpoint_id(before)
        current_checkpoint_step = _snapshot_checkpoint_step(before)
    mark_interrupt_resumed(
        execution,
        workflow_interrupt,
        response_payload=response_payload,
        user_id=user_id,
    )
    next_interrupt = await record_graph_result(
        session,
        execution=execution,
        result=result,
        checkpoint_id=current_checkpoint_id,
        checkpoint_step=current_checkpoint_step,
    )
    if next_interrupt is not None and next_interrupt.manual_task_id is None:
        await _enqueue_resume_job(session, execution=execution, workflow_interrupt=next_interrupt)
    return EmailTicketState(**result)


def _snapshot_checkpoint_id(snapshot) -> str | None:
    config = getattr(snapshot, "config", None) or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    value = configurable.get("checkpoint_id") if isinstance(configurable, dict) else None
    return str(value) if value else None


def _workflow_error_code(exc: Exception) -> str:
    error_text = str(exc)
    return (
        error_text[:100]
        if error_text.startswith("WORKFLOW_")
        else exc.__class__.__name__[:100]
    )


def _snapshot_checkpoint_step(snapshot) -> int | None:
    metadata = getattr(snapshot, "metadata", None) or {}
    value = metadata.get("step") if isinstance(metadata, dict) else None
    return int(value) if isinstance(value, int) else None


def _snapshot_interrupts(snapshot) -> list:
    return [
        item
        for task in (getattr(snapshot, "tasks", None) or ())
        for item in (getattr(task, "interrupts", None) or ())
    ]


def _stable_execution_result(execution: WorkflowExecution, snapshot) -> dict:
    """Verify MySQL's stable ledger against the exact PostgreSQL checkpoint."""
    persisted_id = execution.checkpoint_id
    persisted_step = execution.checkpoint_step
    current_id = _snapshot_checkpoint_id(snapshot)
    current_step = _snapshot_checkpoint_step(snapshot)
    if persisted_id is None or persisted_step is None or current_id is None:
        raise RuntimeError("WORKFLOW_CHECKPOINT_MISSING")
    if current_step is None:
        raise RuntimeError("WORKFLOW_CHECKPOINT_ORDER_UNVERIFIABLE")
    if current_step < persisted_step:
        raise RuntimeError("WORKFLOW_CHECKPOINT_REGRESSION")
    if current_step > persisted_step:
        raise RuntimeError("WORKFLOW_CHECKPOINT_ADVANCED_UNRECORDED")
    if current_id != persisted_id:
        raise RuntimeError("WORKFLOW_CHECKPOINT_DIVERGED")

    values = dict(getattr(snapshot, "values", None) or {})
    if not values:
        raise RuntimeError("WORKFLOW_CHECKPOINT_STATE_MISSING")
    snapshot_execution_id = values.get("execution_id")
    if snapshot_execution_id != execution.execution_id:
        raise RuntimeError("WORKFLOW_CHECKPOINT_EXECUTION_MISMATCH")
    interrupts = _snapshot_interrupts(snapshot)
    next_nodes = tuple(getattr(snapshot, "next", None) or ())
    if execution.status == "completed":
        if interrupts or next_nodes:
            raise RuntimeError("WORKFLOW_CHECKPOINT_STATE_CONFLICT")
    elif not interrupts:
        raise RuntimeError("WORKFLOW_CHECKPOINT_INTERRUPT_MISSING")
    if interrupts:
        values["__interrupt__"] = interrupts
    return values


async def _verified_stable_execution_result(
    session: AsyncSession,
    execution: WorkflowExecution,
    snapshot,
) -> dict:
    values = _stable_execution_result(execution, snapshot)
    interrupts = _snapshot_interrupts(snapshot)
    if execution.status == "completed":
        active_ledger = await session.scalar(
            select(WorkflowInterrupt).where(
                WorkflowInterrupt.execution_id == execution.execution_id,
                WorkflowInterrupt.status.in_(("pending", "resume_queued")),
            )
        )
        if active_ledger is not None:
            raise RuntimeError("WORKFLOW_INTERRUPT_LEDGER_CONFLICT")
        return values

    if len(interrupts) != 1:
        raise RuntimeError("WORKFLOW_CHECKPOINT_INTERRUPT_CONFLICT")
    interrupt_id = str(interrupts[0].id)
    ledger = await session.scalar(
        select(WorkflowInterrupt).where(
            WorkflowInterrupt.execution_id == execution.execution_id,
            WorkflowInterrupt.interrupt_id == interrupt_id,
        )
    )
    if ledger is None:
        raise RuntimeError("WORKFLOW_INTERRUPT_LEDGER_MISSING")
    expected_status = "resume_queued" if execution.status == "resume_queued" else "pending"
    if ledger.status != expected_status:
        raise RuntimeError("WORKFLOW_INTERRUPT_LEDGER_STATUS_CONFLICT")
    if (
        ledger.checkpoint_id != execution.checkpoint_id
        or ledger.checkpoint_step != execution.checkpoint_step
    ):
        raise RuntimeError("WORKFLOW_INTERRUPT_LEDGER_CHECKPOINT_CONFLICT")
    return values


async def _enqueue_resume_job(session, *, execution, workflow_interrupt) -> None:
    payload = workflow_interrupt.request_payload or {}
    job = await enqueue_job(
        session,
        job_type="graph_resume",
        resource_type="workflow_execution",
        resource_id=execution.id,
        idempotency_key=f"graph_resume:{execution.execution_id}:{workflow_interrupt.interrupt_id}",
        metadata={
            "execution_id": execution.execution_id,
            "interrupt_id": workflow_interrupt.interrupt_id,
            "response_payload": {"reason": "scheduled_poll"},
        },
        max_attempts=3,
    )
    delay = max(60, int(payload.get("next_poll_seconds") or 300))
    job.next_run_at = utcnow() + timedelta(seconds=delay)
