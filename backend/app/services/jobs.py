from __future__ import annotations

import asyncio
import socket
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.request_context import get_correlation_id
from app.models import JobRunLog
from app.services.audit import log_system_event
from app.services.common import model_to_dict, utcnow
from app.services.logging_safety import safe_error_code, sanitize_log_payload


JOB_TYPES = {
    "email_parse", "email_reparse", "imap_fetch", "smtp_send", "auto_followup",
    "master_data_import", "export_generate", "relay_ticket_export", "sap_rma_poll",
    "rma_authorization", "rma_archive", "graph_start", "graph_resume",
    "oss_delete",
}
TERMINAL_STATUSES = {"success", "needs_manual_review", "failed", "cancelled"}
NON_RETRYABLE_ERROR_PARTS = {
    "NOT_FOUND", "NOT_SUPPORTED", "REQUIRED", "INVALID", "FORBIDDEN",
    "RECIPIENT_NOT_ALLOWED", "SELECTION", "FOLLOWUP_LIMIT", "TOO_LARGE",
    "TOO_MANY", "ENCRYPTED", "CORRUPT", "UNCERTAIN",
}
JOB_FIELDS = (
    "id", "job_name", "job_type", "status", "resource_type", "resource_id",
    "correlation_id", "started_at", "finished_at", "duration_ms", "processed_count",
    "success_count", "failed_count", "attempt_count", "max_attempts", "next_run_at",
    "locked_at", "error_code", "result_json", "input_oss_object_id",
    "output_oss_object_id", "created_at", "updated_at",
)


class JobLeaseLost(RuntimeError):
    """The worker may no longer mutate a job after its fencing token changes."""


def _new_job_owner_token(worker_id: str | None = None) -> str:
    owner_prefix = (worker_id or socket.gethostname())[:60]
    return f"{owner_prefix}:{uuid4().hex}"[:100]


def serialize_job(job: JobRunLog) -> dict[str, Any]:
    return model_to_dict(job, JOB_FIELDS)


def _job_error_is_retryable(error_code: str) -> bool:
    return not any(part in error_code for part in NON_RETRYABLE_ERROR_PARTS)


async def enqueue_job(
    session: AsyncSession,
    *,
    job_type: str,
    resource_type: str | None,
    resource_id: int | None,
    idempotency_key: str,
    metadata: dict[str, Any] | None = None,
    max_attempts: int = 3,
    input_oss_object_id: int | None = None,
    correlation_id: str | None = None,
    reactivate_terminal: bool = False,
) -> JobRunLog:
    if job_type not in JOB_TYPES:
        raise ValueError("JOB_TYPE_NOT_SUPPORTED")
    existing = await session.scalar(select(JobRunLog).where(JobRunLog.idempotency_key == idempotency_key))
    if existing is not None:
        if reactivate_terminal and existing.status in {"failed", "needs_manual_review", "cancelled"}:
            existing.status = "queued"
            existing.max_attempts = max(int(existing.max_attempts or 0), int(existing.attempt_count or 0) + max(1, max_attempts))
            existing.next_run_at = None
            existing.finished_at = None
            existing.error_code = None
            existing.error_message = None
            existing.metadata_json = sanitize_log_payload(metadata or existing.metadata_json or {})
            existing.updated_at = utcnow()
        return existing
    correlation_id = correlation_id or get_correlation_id()
    created_at = utcnow()
    job = JobRunLog(
        job_name=job_type,
        job_type=job_type,
        status="queued",
        resource_type=resource_type,
        resource_id=resource_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key[:191],
        max_attempts=max(1, max_attempts),
        processed_count=0,
        success_count=0,
        failed_count=0,
        attempt_count=0,
        metadata_json=sanitize_log_payload(metadata or {}),
        input_oss_object_id=input_oss_object_id,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(job)
    await session.flush()
    await log_system_event(
        session,
        event_type="job_queued",
        module_name="jobs",
        event_stage="job",
        event_status="queued",
        target_type=resource_type,
        target_id=resource_id,
        job_run_id=job.id,
        correlation_id=correlation_id,
        message="Background job queued",
        details={"job_type": job_type},
    )
    return job


async def recover_stale_jobs(session: AsyncSession) -> int:
    stale_before = utcnow() - timedelta(seconds=settings.ASYNC_JOB_STALE_SECONDS)
    stale_jobs = (
        await session.execute(
            select(JobRunLog)
            .where(
                JobRunLog.status == "running",
                or_(
                    JobRunLog.locked_at < stale_before,
                    (JobRunLog.locked_at.is_(None) & (JobRunLog.started_at < stale_before)),
                ),
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    now = utcnow()
    for job in stale_jobs:
        if job.locked_at is None:
            job.status = "failed"
            job.error_code = "JOB_ORPHANED_NO_LOCK"
            job.error_message = "Legacy running job had no owner lock and was terminated without replay."
            job.finished_at = now
            job.next_run_at = None
        elif job.job_type in {"graph_start", "graph_resume"}:
            # A stale heartbeat proves that ownership is uncertain, not that
            # the previous graph invocation has stopped. Automatic replay
            # could concurrently advance the same checkpoint thread.
            job.status = "needs_manual_review"
            job.error_code = "GRAPH_JOB_LEASE_EXPIRED_UNCERTAIN"
            job.error_message = "Graph worker lease expired; verify the previous execution stopped before retry."
            job.finished_at = now
            job.next_run_at = None
        elif job.attempt_count < job.max_attempts:
            job.status = "retry_wait"
            job.error_code = "JOB_STALE_LOCK_RECOVERED"
            job.error_message = "Expired worker lock recovered for retry."
            job.next_run_at = now
        else:
            job.status = "failed"
            job.error_code = "JOB_STALE_RETRY_EXHAUSTED"
            job.error_message = "Expired worker lock reached the retry limit."
            job.finished_at = now
            job.next_run_at = None
            await release_terminal_graph_resume(session, job=job, error_code=job.error_code)
        job.locked_at = None
        job.locked_by = None
    return len(stale_jobs)


async def reactivate_stale_graph_job(
    session: AsyncSession,
    *,
    job_id: int,
    operator_user_id: int,
    confirm_previous_worker_stopped: bool,
    reason: str | None = None,
) -> JobRunLog:
    """Explicitly recover an uncertain graph lease after operator fencing confirmation."""
    if not confirm_previous_worker_stopped:
        raise ValueError("GRAPH_PREVIOUS_WORKER_STOP_CONFIRMATION_REQUIRED")
    job = await session.get(JobRunLog, job_id, with_for_update=True)
    if job is None:
        raise LookupError("JOB_NOT_FOUND")
    if (
        job.job_type not in {"graph_start", "graph_resume"}
        or job.status != "needs_manual_review"
        or job.error_code != "GRAPH_JOB_LEASE_EXPIRED_UNCERTAIN"
    ):
        raise ValueError("GRAPH_JOB_NOT_RECOVERABLE_FROM_STALE_LEASE")
    job.status = "queued"
    job.max_attempts = max(int(job.max_attempts or 0), int(job.attempt_count or 0) + 1)
    job.next_run_at = None
    job.finished_at = None
    job.locked_at = None
    job.locked_by = None
    job.error_code = None
    job.error_message = None
    metadata = dict(job.metadata_json or {})
    metadata["lease_recovery"] = {
        "confirmed_by_user_id": operator_user_id,
        "confirmed_at": utcnow().isoformat(),
        "reason": str(reason or "")[:500] or None,
    }
    job.metadata_json = sanitize_log_payload(metadata)
    job.updated_at = utcnow()
    await log_system_event(
        session,
        event_type="graph_job_lease_recovery_confirmed",
        module_name="jobs",
        event_stage="job_recovery",
        event_status="queued",
        target_type=job.resource_type,
        target_id=job.resource_id,
        job_run_id=job.id,
        correlation_id=job.correlation_id,
        message="Operator confirmed the previous graph worker stopped and requeued the job",
        details={"job_type": job.job_type, "operator_user_id": operator_user_id},
    )
    return job


async def reactivate_failed_graph_start_job(
    session: AsyncSession,
    *,
    job_id: int,
    operator_user_id: int,
    reason: str | None = None,
) -> JobRunLog:
    """Retry the same terminal Graph start without creating another execution."""
    job = await session.get(JobRunLog, job_id, with_for_update=True)
    if job is None:
        raise LookupError("JOB_NOT_FOUND")
    if (
        job.job_type != "graph_start"
        or job.status != "failed"
        or job.resource_type != "email"
        or job.resource_id is None
    ):
        raise ValueError("FAILED_GRAPH_START_JOB_NOT_RECOVERABLE")
    metadata = dict(job.metadata_json or {})
    execution_id = str(metadata.get("execution_id") or "")
    if not execution_id:
        raise ValueError("GRAPH_START_EXECUTION_ID_REQUIRED")
    from app.models import WorkflowExecution

    execution = await session.scalar(
        select(WorkflowExecution)
        .where(WorkflowExecution.execution_id == execution_id)
        .with_for_update()
    )
    if execution is not None and (
        execution.execution_mode != "langgraph"
        or execution.email_id != job.resource_id
        or execution.trigger_job_id not in {None, job.id}
        or execution.status != "failed"
    ):
        raise ValueError("FAILED_GRAPH_START_EXECUTION_CONFLICT")
    if execution is not None and execution.trigger_job_id is None:
        execution.trigger_job_id = job.id
    job.status = "queued"
    job.max_attempts = max(int(job.max_attempts or 0), int(job.attempt_count or 0) + 1)
    job.next_run_at = None
    job.finished_at = None
    job.locked_at = None
    job.locked_by = None
    job.error_code = None
    job.error_message = None
    metadata["terminal_recovery"] = {
        "requested_by_user_id": operator_user_id,
        "requested_at": utcnow().isoformat(),
        "reason": str(reason or "")[:500] or None,
    }
    job.metadata_json = sanitize_log_payload(metadata)
    job.updated_at = utcnow()
    await log_system_event(
        session,
        event_type="failed_graph_start_recovery_requested",
        module_name="jobs",
        event_stage="job_recovery",
        event_status="queued",
        target_type=job.resource_type,
        target_id=job.resource_id,
        job_run_id=job.id,
        correlation_id=job.correlation_id,
        message="Operator requeued the same failed graph start execution",
        details={"job_type": job.job_type, "operator_user_id": operator_user_id},
    )
    return job


async def release_terminal_graph_resume(
    session: AsyncSession,
    *,
    job: JobRunLog,
    error_code: str | None,
) -> bool:
    """Release a terminal resume lease so a human can explicitly retry it."""
    if job.job_type != "graph_resume":
        return False
    metadata = job.metadata_json or {}
    execution_id = str(metadata.get("execution_id") or "")
    interrupt_id = str(metadata.get("interrupt_id") or "")
    if not execution_id or not interrupt_id:
        return False
    from app.models import ManualReviewTask, WorkflowExecution, WorkflowInterrupt

    pair = (
        await session.execute(
            select(WorkflowExecution, WorkflowInterrupt)
            .join(WorkflowInterrupt, WorkflowInterrupt.execution_id == WorkflowExecution.execution_id)
            .where(
                WorkflowExecution.execution_id == execution_id,
                WorkflowInterrupt.interrupt_id == interrupt_id,
                WorkflowInterrupt.status.in_({"pending", "resume_queued"}),
            )
            .with_for_update()
        )
    ).first()
    if pair is None:
        return False
    execution, workflow_interrupt = pair
    workflow_interrupt.status = "pending"
    workflow_interrupt.error_message = str(error_code or "GRAPH_RESUME_FAILED")[:2000]
    execution.status = "waiting_human" if workflow_interrupt.manual_task_id else "waiting_external"
    execution.last_error_code = str(error_code or "GRAPH_RESUME_FAILED")[:100]
    if workflow_interrupt.manual_task_id is not None:
        task = await session.get(ManualReviewTask, workflow_interrupt.manual_task_id, with_for_update=True)
        if task is not None and task.status in {"resolved", "closed"}:
            task.status = "pending"
            task.resolved_by_user_id = None
            task.resolved_at = None
            task.resolution = "LangGraph resume failed; review decision retained in workflow interrupt ledger."
    return True


async def claim_next_job(session: AsyncSession, *, worker_id: str | None = None) -> JobRunLog | None:
    now = utcnow()
    await recover_stale_jobs(session)
    statement = (
        select(JobRunLog)
        .where(
            JobRunLog.status.in_(["queued", "retry_wait"]),
            or_(JobRunLog.next_run_at.is_(None), JobRunLog.next_run_at <= now),
        )
        .order_by(JobRunLog.created_at.asc(), JobRunLog.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = await session.scalar(statement)
    if job is None:
        return None
    job.status = "running"
    job.started_at = job.started_at or now
    job.locked_at = now
    job.locked_by = _new_job_owner_token(worker_id)
    job.attempt_count += 1
    job.next_run_at = None
    await session.flush()
    return job


async def renew_job_lease(
    session: AsyncSession,
    *,
    job_id: int,
    owner_token: str,
) -> bool:
    """Refresh one live lease without extending a superseded worker's claim."""
    result = await session.execute(
        update(JobRunLog)
        .where(
            JobRunLog.id == job_id,
            JobRunLog.status == "running",
            JobRunLog.locked_by == owner_token,
        )
        .values(locked_at=utcnow(), updated_at=utcnow())
    )
    return int(result.rowcount or 0) == 1


async def _lock_owned_job(
    session: AsyncSession,
    *,
    job_id: int,
    owner_token: str,
) -> JobRunLog:
    job = await session.scalar(
        select(JobRunLog)
        .where(JobRunLog.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None or job.status != "running" or job.locked_by != owner_token:
        raise JobLeaseLost("JOB_LEASE_LOST")
    return job


async def _execute_job_command(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    metadata = job.metadata_json or {}
    user_id = metadata.get("user_id") if isinstance(metadata.get("user_id"), int) else None
    if job.job_type == "graph_start":
        from app.workflows.checkpoint import postgres_checkpointer
        from app.workflows.email_ticket.runner import run_active_email_ticket_workflow

        if job.resource_id is None:
            raise ValueError("JOB_RESOURCE_REQUIRED")
        async with postgres_checkpointer(
            settings.LANGGRAPH_CHECKPOINT_DATABASE_URL,
            setup=settings.LANGGRAPH_CHECKPOINT_AUTO_SETUP,
            strict_msgpack=settings.LANGGRAPH_STRICT_MSGPACK,
        ) as checkpointer:
            result = await run_active_email_ticket_workflow(
                session,
                checkpointer=checkpointer,
                email_id=job.resource_id,
                execution_id=str(metadata.get("execution_id") or "") or None,
                trigger_job_id=job.id,
                parse_request={
                    "user_id": user_id,
                    "reason": metadata.get("reason"),
                    "durable_attachment_stages": bool(metadata.get("durable_attachment_stages")),
                    "rule_parse_result_id": metadata.get("rule_parse_result_id"),
                },
            )
        return {
            "status": "completed" if result.get("execution_state") == "completed" else "started",
            "execution_id": result.get("execution_id"),
            "execution_state": result.get("execution_state"),
        }
    if job.job_type == "graph_resume":
        from app.workflows.checkpoint import postgres_checkpointer
        from app.workflows.email_ticket.runner import resume_active_email_ticket_workflow

        execution_id = str(metadata.get("execution_id") or "")
        interrupt_id = str(metadata.get("interrupt_id") or "")
        if not execution_id or not interrupt_id:
            raise ValueError("WORKFLOW_RESUME_IDENTIFIERS_REQUIRED")
        async with postgres_checkpointer(
            settings.LANGGRAPH_CHECKPOINT_DATABASE_URL,
            setup=settings.LANGGRAPH_CHECKPOINT_AUTO_SETUP,
            strict_msgpack=settings.LANGGRAPH_STRICT_MSGPACK,
        ) as checkpointer:
            result = await resume_active_email_ticket_workflow(
                session,
                checkpointer=checkpointer,
                execution_id=execution_id,
                interrupt_id=interrupt_id,
                response_payload=dict(metadata.get("response_payload") or {}),
                user_id=user_id,
            )
        return {
            "status": "completed" if result.get("execution_state") == "completed" else "resumed",
            "execution_id": execution_id,
            "execution_state": result.get("execution_state"),
        }
    if job.job_type in {"email_parse", "email_reparse"}:
        from app.services.emails import dispatch_email_parse

        if job.resource_id is None:
            raise ValueError("JOB_RESOURCE_REQUIRED")
        return await dispatch_email_parse(
            session, email_id=job.resource_id, user_id=user_id,
            reason="background parse",
            durable_attachment_stages=True,
            workflow_execution_id=f"email-{job.resource_id}-job-{job.id}",
            rule_parse_result_id=(
                int(metadata["rule_parse_result_id"])
                if isinstance(metadata.get("rule_parse_result_id"), int)
                else None
            ),
        )
    if job.job_type == "imap_fetch":
        from app.services.imap_fetcher import run_imap_fetch_locked

        return await run_imap_fetch_locked(
            session,
            tracking_job=job,
            busy_is_error=True,
            folder_name=str(metadata.get("folder_name") or settings.IMAP_FOLDER),
            limit=int(metadata.get("limit") or settings.IMAP_FETCH_LIMIT),
            unseen_only=bool(metadata.get("unseen_only", settings.IMAP_UNSEEN_ONLY)),
            message_id=metadata.get("message_id"),
            auto_parse=bool(metadata.get("auto_parse", True)),
            archive_to_oss=True,
            user_id=user_id,
        )
    if job.job_type == "smtp_send":
        from app.services.replies import approve_reply

        if job.resource_id is None or user_id is None:
            raise ValueError("JOB_RESOURCE_REQUIRED")
        return await approve_reply(session, reply_id=job.resource_id, user_id=user_id)
    if job.job_type == "relay_ticket_export":
        from app.services.relay_jobs import execute_ticket_relay_export

        if job.resource_id is None:
            raise ValueError("JOB_RESOURCE_REQUIRED")
        return await execute_ticket_relay_export(session, export_id=job.resource_id)
    if job.job_type == "sap_rma_poll":
        from app.services.sap_rma import poll_export_batch

        if job.resource_id is None:
            raise ValueError("JOB_RESOURCE_REQUIRED")
        return await poll_export_batch(session, export_id=job.resource_id)
    if job.job_type == "rma_authorization":
        from app.services.replies import create_and_send_rma_authorization

        if job.resource_id is None:
            raise ValueError("JOB_RESOURCE_REQUIRED")
        return await create_and_send_rma_authorization(
            session,
            ticket_id=job.resource_id,
            user_id=user_id,
            expected_version=int(metadata.get("ticket_version") or 0),
            expected_safety_hash=str(metadata.get("safety_check_hash") or ""),
            expected_sn_validation_hash=str(metadata.get("sn_validation_hash") or ""),
            expected_rma_template_version=str(metadata.get("rma_template_version") or ""),
            expected_rma_no=str(metadata.get("rma_no") or ""),
        )
    if job.job_type == "rma_archive":
        from app.services.replies import retry_rma_archive

        if job.resource_id is None:
            raise ValueError("JOB_RESOURCE_REQUIRED")
        return await retry_rma_archive(
            session,
            reply_id=job.resource_id,
            user_id=user_id,
        )
    if job.job_type == "auto_followup":
        from app.services.replies import create_reply_draft

        if job.resource_id is None:
            raise ValueError("JOB_RESOURCE_REQUIRED")
        return await create_reply_draft(session, ticket_id=job.resource_id, user_id=user_id)
    if job.job_type == "oss_delete":
        from app.services.deletions import process_oss_deletion_operation

        if job.resource_id is None:
            raise ValueError("JOB_RESOURCE_REQUIRED")
        return await process_oss_deletion_operation(session, job.resource_id)
    if job.job_type == "master_data_import":
        from app.services import master_data
        from app.services.storage import download_oss_object_bytes

        if job.input_oss_object_id is None or user_id is None:
            raise ValueError("JOB_RESOURCE_REQUIRED")
        content = await download_oss_object_bytes(session, oss_object_id=job.input_oss_object_id)
        kind = metadata.get("kind")
        if kind == "sn_assets":
            items, file_hash = await asyncio.to_thread(master_data.parse_sn_assets_xlsx, content)
            return await master_data.import_sn_assets(
                session, items=items, source_file_name=None, source_file_hash=file_hash,
                user_id=user_id, job=job,
            )
        if kind == "board_cards":
            items, file_hash = await asyncio.to_thread(
                master_data.parse_board_cards_file,
                content,
                filename=metadata.get("filename"),
            )
            return await master_data.import_board_cards(
                session,
                items=items,
                source_file_name=metadata.get("filename"),
                source_file_hash=file_hash,
                user_id=user_id, job=job,
            )
        raise ValueError("MASTER_DATA_KIND_NOT_SUPPORTED")
    if job.job_type == "export_generate":
        from app.services import emails, master_data, tickets
        from app.services.storage import upload_bytes_to_oss

        kind = metadata.get("kind")
        filters = metadata.get("filters") if isinstance(metadata.get("filters"), dict) else {}
        ids = metadata.get("ids") if isinstance(metadata.get("ids"), list) else []
        if kind == "sn_assets":
            content = await (
                master_data.export_sn_assets_selected(session, ids=ids)
                if ids else master_data.export_sn_assets(session, **filters)
            )
        elif kind == "board_cards":
            content = await (
                master_data.export_board_cards_selected(session, ids=ids)
                if ids else master_data.export_board_cards(session, **filters)
            )
        elif kind == "emails":
            rows = await emails.export_emails(session, **filters)
            fields = [
                "id", "message_id", "subject", "from_address", "to_addresses", "intent_type",
                "parse_status", "received_at", "attachment_count", "latest_parser_type",
                "latest_confidence_score", "latest_missing_fields", "latest_conflict_fields",
            ]
            content = await asyncio.to_thread(master_data.xlsx_bytes, rows, fields)
        elif kind == "tickets":
            if ids:
                content = await tickets.export_tickets_selected(session, ids=ids)
            else:
                rows = await tickets.export_tickets(session, **filters)
                fields = [
                    "ticket_no", "current_status_code", "customer_code", "customer_name",
                    "contact_person", "contact_phone", "contact_email", "request_date",
                    "assigned_user_id", "followup_count", "confidence_score", "missing_fields_json",
                    "conflict_fields_json", "attachment_summary", "sn_validation_summary",
                    "reply_status_summary", "created_at", "updated_at",
                ]
                content = await asyncio.to_thread(master_data.xlsx_bytes, rows, fields)
        else:
            raise ValueError("EXPORT_KIND_NOT_SUPPORTED")
        output = await upload_bytes_to_oss(
            session,
            content=content,
            original_file_name=f"{kind}-export.xlsx",
            content_type=master_data.EXCEL_MEDIA_TYPE,
            source_type="generated_export",
            user_id=user_id,
        )
        job.output_oss_object_id = output.id
        return {"kind": kind, "output_oss_object_id": output.id, "file_size": len(content)}
    raise NotImplementedError(f"{job.job_type.upper()}_HANDLER_NOT_IMPLEMENTED")


async def execute_claimed_job(
    session: AsyncSession,
    job: JobRunLog,
    *,
    expected_owner_token: str | None = None,
) -> JobRunLog:
    started = utcnow()
    job_id = job.id
    owner_token = str(expected_owner_token or job.locked_by or "")
    if not owner_token:
        raise JobLeaseLost("JOB_LEASE_TOKEN_MISSING")
    if job.status != "running" or job.locked_by != owner_token:
        raise JobLeaseLost("JOB_LEASE_LOST_BEFORE_EXECUTION")
    lease_lost = False
    try:
        result = await _execute_job_command(session, job)
        # IMAP processes each message durably and may roll back one failed
        # message. Reload the queue row before reading or writing it so an
        # expired ORM instance cannot trigger implicit async IO.
        job = await _lock_owned_job(
            session,
            job_id=job_id,
            owner_token=owner_token,
        )
        business_status = str(result.get("status") or "") if isinstance(result, dict) else ""
        if business_status == "waiting_rma":
            job.status = "retry_wait"
            job.result_json = sanitize_log_payload(result)
            job.error_code = None
            job.error_message = None
            job.next_run_at = utcnow() + timedelta(
                seconds=max(60, int(result.get("next_poll_seconds") or 300))
            )
        elif business_status == "superseded":
            job.status = "superseded"
            job.result_json = sanitize_log_payload(result)
            job.error_code = "TASK_SNAPSHOT_SUPERSEDED"
            job.error_message = None
            job.finished_at = utcnow()
        elif business_status in {"failed", "send_failed", "manual_review", "send_uncertain", "misconfigured", "archive_failed"}:
            error_code = str(result.get("error_code") or business_status).upper()
            retryable = (
                job.job_type in {"relay_ticket_export", "smtp_send", "rma_archive", "oss_delete"}
                and _job_error_is_retryable(error_code)
            )
            job.result_json = sanitize_log_payload(result)
            job.error_code = error_code
            job.error_message = str(result.get("error_message") or error_code)[:2000]
            job.failed_count += 1
            if retryable and job.attempt_count < job.max_attempts:
                job.status = "retry_wait"
                delay_minutes = (5, 15, 60)[min(job.attempt_count - 1, 2)]
                job.next_run_at = utcnow() + timedelta(minutes=delay_minutes)
            else:
                job.status = "needs_manual_review" if business_status in {"manual_review", "send_uncertain", "misconfigured", "archive_failed"} else "failed"
                job.finished_at = utcnow()
        else:
            job.status = "success"
            job.success_count = int(result.get("success_count") or 1) if isinstance(result, dict) else 1
            job.failed_count = int(result.get("failed_count") or 0) if isinstance(result, dict) else 0
            job.result_json = sanitize_log_payload(result)
            job.error_code = None
            job.error_message = None
    except asyncio.CancelledError:
        lease_lost = True
        await session.rollback()
        raise
    except JobLeaseLost:
        lease_lost = True
        await session.rollback()
        raise
    except Exception as exc:
        class_error_codes = {
            "TypeError": "JOB_TYPE_ERROR",
            "StatementError": "DB_STATEMENT_ERROR",
            "IntegrityError": "DB_INTEGRITY_ERROR",
        }
        error_code = class_error_codes.get(exc.__class__.__name__) or safe_error_code(
            exc, exc.__class__.__name__.upper()
        ) or "JOB_FAILED"
        original = getattr(exc, "orig", None)
        diagnostic = exc.__class__.__name__
        if original is not None:
            diagnostic = f"{diagnostic}:{original.__class__.__name__}"
            original_args = getattr(original, "args", ())
            if original_args and isinstance(original_args[0], int):
                diagnostic = f"{diagnostic}:vendor_code={original_args[0]}"
        await session.rollback()
        try:
            job = await _lock_owned_job(
                session,
                job_id=job_id,
                owner_token=owner_token,
            )
        except JobLeaseLost:
            lease_lost = True
            raise
        retryable = _job_error_is_retryable(error_code)
        if retryable and job.attempt_count < job.max_attempts:
            job.status = "retry_wait"
            delay_minutes = (5, 15, 60)[min(job.attempt_count - 1, 2)]
            job.next_run_at = utcnow() + timedelta(minutes=delay_minutes)
        else:
            job.status = "needs_manual_review" if error_code.startswith("SMTP_") else "failed"
            job.finished_at = utcnow()
        job.failed_count += 1
        job.error_code = error_code
        job.error_message = diagnostic
        if job.status in {"failed", "needs_manual_review"}:
            await release_terminal_graph_resume(session, job=job, error_code=error_code)
    finally:
        if not lease_lost:
            job.duration_ms = int((utcnow() - started).total_seconds() * 1000)
            if job.status == "success":
                job.finished_at = utcnow()
            job.locked_at = None
            job.locked_by = None
            await log_system_event(
                session,
                event_type="job_completed",
                module_name="jobs",
                event_stage="job",
                event_status=job.status,
                target_type=job.resource_type,
                target_id=job.resource_id,
                job_run_id=job.id,
                correlation_id=job.correlation_id,
                duration_ms=job.duration_ms,
                error_code=job.error_code,
                severity="info" if job.status == "success" else "error",
                message="Background job execution completed",
                details={"job_type": job.job_type, "attempt_count": job.attempt_count},
            )
    return job
