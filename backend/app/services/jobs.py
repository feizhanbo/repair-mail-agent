from __future__ import annotations

import logging
import re
import socket
import traceback
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.request_context import get_correlation_id
from app.models import JobRunLog
from app.services.audit import log_system_event
from app.services.common import model_to_dict, utcnow
from app.services.logging_safety import safe_error_code, sanitize_log_payload
from app.services.worker_fencing import JobOwnershipLost


logger = logging.getLogger(__name__)


JOB_TYPES = {
    "email_parse", "email_reparse", "imap_fetch", "mail_ingress_process", "smtp_send", "auto_followup",
    "master_data_import", "export_generate", "relay_ticket_export", "sap_rma_poll",
    "ai_log_maintenance", "consistency_recovery", "sap_sn_sync",
    "rma_authorization", "rma_archive",
    "oss_delete", "sap_submit_reconcile",
}
MAIL_JOB_TYPES = {
    "imap_fetch",
    "mail_ingress_process",
    "email_parse",
    "email_reparse",
    "smtp_send",
    "auto_followup",
    "rma_authorization",
    "rma_archive",
}
TERMINAL_STATUSES = {"success", "needs_manual_review", "failed", "cancelled", "superseded"}
ACTIVE_STATUSES = {"queued", "running", "retry_wait"}
JOB_PRIORITY = {
    "smtp_send": 0,
    "rma_authorization": 0,
    "rma_archive": 0,
    "imap_fetch": 1,
    "mail_ingress_process": 1,
    "email_parse": 1,
    "email_reparse": 1,
    "auto_followup": 1,
    "relay_ticket_export": 2,
    "sap_rma_poll": 2,
    "sap_submit_reconcile": 0,
    "master_data_import": 3,
    "export_generate": 3,
    "sap_sn_sync": 3,
    "ai_log_maintenance": 3,
    "consistency_recovery": 3,
    "oss_delete": 3,
}
JOB_TIMEOUT_SECONDS = {
    "smtp_send": 120,
    "sap_rma_poll": 300,
    "sap_submit_reconcile": 300,
    "relay_ticket_export": 300,
    "email_parse": 600,
    "email_reparse": 600,
    "rma_authorization": 600,
    "rma_archive": 600,
    "imap_fetch": 900,
    "mail_ingress_process": 900,
    "auto_followup": 600,
    "master_data_import": 120,
    "export_generate": 120,
    "sap_sn_sync": 120,
    "ai_log_maintenance": 120,
    "consistency_recovery": 120,
    "oss_delete": 120,
}
HIGH_PRIORITY_STREAK_LIMIT = 20
LOW_PRIORITY_MAX_WAIT_SECONDS = 15 * 60
NON_RETRYABLE_ERROR_PARTS = {
    "NOT_FOUND", "NOT_SUPPORTED", "NOT_IMPLEMENTED", "REQUIRED", "INVALID", "FORBIDDEN",
    "RECIPIENT_NOT_ALLOWED", "SELECTION", "FOLLOWUP_LIMIT", "TOO_LARGE",
    "TOO_MANY", "ENCRYPTED", "CORRUPT", "UNCERTAIN", "TERMINAL",
}
JOB_FIELDS = (
    "id", "job_name", "job_type", "status", "resource_type", "resource_id",
    "correlation_id", "started_at", "finished_at", "duration_ms", "processed_count",
    "success_count", "failed_count", "attempt_count", "max_attempts", "next_run_at",
    "locked_at", "priority", "retry_of_job_id", "execution_deadline_at",
    "error_code", "result_json", "input_oss_object_id",
    "output_oss_object_id", "created_at", "updated_at",
)


def serialize_job(job: JobRunLog) -> dict[str, Any]:
    return model_to_dict(job, JOB_FIELDS)


def _job_error_is_retryable(error_code: str) -> bool:
    return not any(part in error_code for part in NON_RETRYABLE_ERROR_PARTS)


def _job_retry_delay(job_type: str, attempt_count: int) -> timedelta:
    if job_type == "smtp_send":
        backoff = settings.SMTP_RETRY_BACKOFF_SECONDS or [30, 120, 300]
        return timedelta(seconds=max(1, int(backoff[min(max(0, attempt_count - 1), len(backoff) - 1)])))
    delay_minutes = (5, 15, 60)[min(max(0, attempt_count - 1), 2)]
    return timedelta(minutes=delay_minutes)


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
    priority: int | None = None,
    retry_of_job_id: int | None = None,
) -> JobRunLog:
    if job_type not in JOB_TYPES:
        raise ValueError("JOB_TYPE_NOT_SUPPORTED")
    normalized_key = idempotency_key[:191]
    existing = await session.scalar(select(JobRunLog).where(JobRunLog.idempotency_key == normalized_key))
    if existing is not None:
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
        idempotency_key=normalized_key,
        priority=max(0, min(3, priority if priority is not None else JOB_PRIORITY[job_type])),
        retry_of_job_id=retry_of_job_id,
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
    try:
        # The initial lookup is intentionally not a gap lock: concurrent API
        # requests for unrelated keys must remain independent.  The savepoint
        # lets the unique index settle a same-key race without poisoning the
        # caller's outer transaction.
        if hasattr(session, "begin_nested"):
            async with session.begin_nested():
                session.add(job)
                await session.flush()
        else:  # Lightweight unit-test sessions do not implement transactions.
            session.add(job)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(JobRunLog)
            .where(JobRunLog.idempotency_key == normalized_key)
            .with_for_update()
        )
        if existing is not None:
            return existing
        raise
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


async def enqueue_job_retry(
    session: AsyncSession,
    *,
    previous_job_id: int,
    metadata: dict[str, Any] | None = None,
    max_attempts: int | None = None,
    input_oss_object_id: int | None = None,
) -> JobRunLog:
    """Create one immutable retry child for a terminal job.

    Locking the parent plus the unique retry_of_job_id constraint makes a
    concurrent manual retry idempotent without rewriting the old execution
    record.
    """
    previous = await session.get(JobRunLog, previous_job_id, with_for_update=True)
    if previous is None:
        raise ValueError("JOB_RETRY_PARENT_NOT_FOUND")
    existing_child = await session.scalar(
        select(JobRunLog)
        .where(JobRunLog.retry_of_job_id == previous.id)
        .with_for_update()
    )
    if existing_child is not None:
        return existing_child
    if previous.status not in TERMINAL_STATUSES:
        return previous
    if previous.status == "success":
        return previous
    return await enqueue_job(
        session,
        job_type=previous.job_type,
        resource_type=previous.resource_type,
        resource_id=previous.resource_id,
        idempotency_key=f"retry:{previous.id}",
        metadata=metadata if metadata is not None else previous.metadata_json,
        max_attempts=max_attempts if max_attempts is not None else previous.max_attempts,
        input_oss_object_id=(
            input_oss_object_id
            if input_oss_object_id is not None
            else previous.input_oss_object_id
        ),
        correlation_id=previous.correlation_id,
        priority=previous.priority,
        retry_of_job_id=previous.id,
    )


async def enqueue_job_or_retry_terminal(
    session: AsyncSession,
    *,
    job_type: str,
    resource_type: str | None,
    resource_id: int | None,
    idempotency_key: str,
    metadata: dict[str, Any] | None = None,
    max_attempts: int = 3,
    input_oss_object_id: int | None = None,
) -> JobRunLog:
    """Reuse an active/successful chain or append one retry to its terminal leaf."""
    normalized_key = idempotency_key[:191]
    current = await session.scalar(
        select(JobRunLog)
        .where(JobRunLog.idempotency_key == normalized_key)
        .with_for_update()
    )
    if current is None:
        return await enqueue_job(
            session,
            job_type=job_type,
            resource_type=resource_type,
            resource_id=resource_id,
            idempotency_key=normalized_key,
            metadata=metadata,
            max_attempts=max_attempts,
            input_oss_object_id=input_oss_object_id,
        )
    while True:
        child = await session.scalar(
            select(JobRunLog)
            .where(JobRunLog.retry_of_job_id == current.id)
            .with_for_update()
        )
        if child is None:
            break
        current = child
    if current.status in ACTIVE_STATUSES or current.status == "success":
        return current
    return await enqueue_job_retry(
        session,
        previous_job_id=current.id,
        metadata=metadata,
        max_attempts=max_attempts,
        input_oss_object_id=input_oss_object_id,
    )


async def recover_stale_jobs(session: AsyncSession) -> int:
    stale_before = utcnow() - timedelta(seconds=settings.ASYNC_JOB_STALE_SECONDS)
    stale_jobs = (
        await session.execute(
            select(JobRunLog)
            .where(
                JobRunLog.status == "running",
                or_(
                    JobRunLog.execution_deadline_at <= utcnow(),
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
        elif job.job_type == "smtp_send":
            job.status = "needs_manual_review"
            job.error_code = "SMTP_DELIVERY_UNCERTAIN"
            job.error_message = "SMTP worker ownership expired while delivery outcome was unknown."
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
        job.locked_at = None
        job.locked_by = None
        job.fencing_token = None
        job.execution_deadline_at = None
    exhausted_jobs = (
        await session.execute(
            select(JobRunLog)
            .where(
                JobRunLog.status.in_(["queued", "retry_wait"]),
                JobRunLog.attempt_count >= JobRunLog.max_attempts,
            )
            .order_by(JobRunLog.id)
            .with_for_update(skip_locked=True)
            .limit(100)
        )
    ).scalars().all()
    for job in exhausted_jobs:
        # Keep the state predicate explicit as a defense against stale ORM
        # identity-map rows and test doubles; only exhausted queued work is
        # eligible for this terminal transition.
        if job.status not in {"queued", "retry_wait"} or job.attempt_count < job.max_attempts:
            continue
        job.status = "failed"
        job.error_code = "JOB_RETRY_EXHAUSTED"
        job.error_message = "Job retry budget was exhausted before another claim."
        job.finished_at = now
        job.next_run_at = None
        job.execution_deadline_at = None
    return len({int(job.id) for job in [*stale_jobs, *exhausted_jobs]})


async def claim_next_job(
    session: AsyncSession,
    *,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    prefer_aged_low_priority: bool = False,
) -> JobRunLog | None:
    now = utcnow()
    eligible = (
        JobRunLog.status.in_(["queued", "retry_wait"]),
        JobRunLog.attempt_count < JobRunLog.max_attempts,
        or_(JobRunLog.next_run_at.is_(None), JobRunLog.next_run_at <= now),
    )
    job = None
    if prefer_aged_low_priority:
        job = await session.scalar(
            select(JobRunLog)
            .where(
                *eligible,
                JobRunLog.priority >= 2,
                JobRunLog.created_at <= now - timedelta(seconds=LOW_PRIORITY_MAX_WAIT_SECONDS),
            )
            .order_by(JobRunLog.created_at.asc(), JobRunLog.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
    statement = (
        select(JobRunLog)
        .where(*eligible)
        .order_by(JobRunLog.priority.asc(), JobRunLog.created_at.asc(), JobRunLog.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        job = await session.scalar(statement)
    if job is None:
        return None
    job.status = "running"
    job.started_at = job.started_at or now
    job.locked_at = now
    job.locked_by = (worker_id or socket.gethostname())[:100]
    job.fencing_token = fencing_token
    job.attempt_count += 1
    job.next_run_at = None
    job.execution_deadline_at = now + timedelta(
        seconds=JOB_TIMEOUT_SECONDS.get(job.job_type, settings.ASYNC_JOB_STALE_SECONDS)
    )
    await session.flush()
    return job


async def mark_interrupted_job(
    session: AsyncSession,
    *,
    job_id: int,
    worker_id: str,
    fencing_token: int,
    error_code: str,
) -> bool:
    """Finalize a child process that timed out or exited abnormally.

    The ownership predicate is the fencing boundary: a stale supervisor cannot
    modify a job after another lease owner has taken over.
    """
    job = await session.scalar(
        select(JobRunLog)
        .where(
            JobRunLog.id == job_id,
            JobRunLog.status == "running",
            JobRunLog.locked_by == worker_id,
            JobRunLog.fencing_token == fencing_token,
        )
        .with_for_update()
    )
    if job is None:
        return False
    now = utcnow()
    job.failed_count += 1
    job.error_code = error_code
    job.error_message = error_code
    job.locked_at = None
    job.locked_by = None
    job.fencing_token = None
    job.execution_deadline_at = None
    if job.job_type == "smtp_send":
        job.status = "needs_manual_review"
        job.error_code = "SMTP_DELIVERY_UNCERTAIN"
        job.error_message = f"{error_code}: delivery outcome is unknown"
        job.finished_at = now
        job.next_run_at = None
    elif job.attempt_count < job.max_attempts:
        job.status = "retry_wait"
        job.next_run_at = now + _job_retry_delay(job.job_type, job.attempt_count)
    else:
        job.status = "failed"
        job.finished_at = now
        job.next_run_at = None
    return True



async def execute_claimed_job(session: AsyncSession, job: JobRunLog) -> JobRunLog:
    from app.services.job_dispatcher import JobOutcomeKind, dispatch_job

    started = utcnow()
    job_id = job.id
    worker_instance = job.locked_by
    expected_fencing_token = job.fencing_token
    ownership_lost = False
    logger.info(
        "Background job started",
        extra={
            "event": "job_started", "job_run_id": job.id, "job_type": job.job_type,
            "resource_type": job.resource_type, "resource_id": job.resource_id,
            "attempt": job.attempt_count, "max_attempt": job.max_attempts,
            "worker_instance": worker_instance,
        },
    )
    try:
        outcome = await dispatch_job(session, job)
        result = outcome.payload
        # IMAP processes each message durably and may roll back one failed
        # message. Reload the queue row before reading or writing it so an
        # expired ORM instance cannot trigger implicit async IO.
        job = (
            await session.scalar(
                select(JobRunLog)
                .where(JobRunLog.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ) or job
        if (
            job.status != "running"
            or job.locked_by != worker_instance
            or job.fencing_token != expected_fencing_token
        ):
            raise JobOwnershipLost("JOB_OWNERSHIP_LOST")
        if outcome.kind == JobOutcomeKind.RETRY:
            job.result_json = sanitize_log_payload(result)
            job.error_code = outcome.error_code
            job.error_message = outcome.error_code
            if job.attempt_count < job.max_attempts:
                job.status = "retry_wait"
                job.next_run_at = utcnow() + timedelta(
                    seconds=max(60, int(outcome.retry_after_seconds or 300))
                )
            else:
                job.status = "failed"
                job.error_code = outcome.error_code or "JOB_RETRY_EXHAUSTED"
                job.error_message = job.error_code
                job.finished_at = utcnow()
        elif outcome.kind == JobOutcomeKind.CONTINUE:
            job.status = "queued"
            job.result_json = sanitize_log_payload(result)
            job.error_code = None
            job.error_message = None
            job.next_run_at = utcnow()
            job.attempt_count = max(0, job.attempt_count - 1)
        elif outcome.kind == JobOutcomeKind.SUPERSEDED:
            job.status = "superseded"
            job.result_json = sanitize_log_payload(result)
            job.error_code = outcome.error_code or "TASK_SNAPSHOT_SUPERSEDED"
            job.error_message = None
            job.finished_at = utcnow()
        elif outcome.kind in {JobOutcomeKind.FAILED, JobOutcomeKind.MANUAL_REVIEW}:
            error_code = outcome.error_code or "JOB_FAILED"
            retryable = (
                job.job_type in {"relay_ticket_export", "smtp_send", "rma_archive", "oss_delete"}
                and outcome.kind == JobOutcomeKind.FAILED
                and _job_error_is_retryable(error_code)
            )
            job.result_json = sanitize_log_payload(result)
            job.error_code = error_code
            job.error_message = str(result.get("error_message") or error_code)[:2000]
            job.failed_count += 1
            if retryable and job.attempt_count < job.max_attempts:
                job.status = "retry_wait"
                job.next_run_at = utcnow() + _job_retry_delay(job.job_type, job.attempt_count)
            else:
                job.status = (
                    "needs_manual_review"
                    if outcome.kind == JobOutcomeKind.MANUAL_REVIEW
                    else "failed"
                )
                job.finished_at = utcnow()
        elif outcome.kind == JobOutcomeKind.SUCCESS:
            job.status = "success"
            job.success_count = int(result.get("success_count") or 1)
            job.failed_count = int(result.get("failed_count") or 0)
            job.result_json = sanitize_log_payload(result)
            job.error_code = None
            job.error_message = None
        else:  # Defensive: a future enum value must fail closed.
            job.status = "failed"
            job.result_json = sanitize_log_payload(result)
            job.error_code = "JOB_OUTCOME_INVALID"
            job.error_message = "JOB_OUTCOME_INVALID"
            job.finished_at = utcnow()
    except JobOwnershipLost:
        ownership_lost = True
        await session.rollback()
        raise
    except Exception as exc:
        logger.exception(
            "Background job execution failed: job_id=%s job_type=%s resource_type=%s resource_id=%s",
            job_id,
            job.job_type,
            job.resource_type,
            job.resource_id,
            extra={
                "event": "job_failed", "job_run_id": job_id, "job_type": job.job_type,
                "attempt": job.attempt_count, "max_attempt": job.max_attempts,
                "worker_instance": worker_instance,
            },
        )
        class_error_codes = {
            "TypeError": "JOB_TYPE_ERROR",
            "StatementError": "DB_STATEMENT_ERROR",
            "IntegrityError": "DB_INTEGRITY_ERROR",
            "NotImplementedError": "JOB_HANDLER_NOT_IMPLEMENTED",
        }
        error_code = class_error_codes.get(exc.__class__.__name__) or safe_error_code(
            exc, exc.__class__.__name__.upper()
        ) or "JOB_FAILED"
        original = getattr(exc, "orig", None)
        diagnostic = f"JOBS_EXCEPTION_V3:{exc.__class__.__name__}"
        if original is not None:
            diagnostic = f"{diagnostic}:{original.__class__.__name__}"
            original_args = getattr(original, "args", ())
            if original_args and isinstance(original_args[0], int):
                diagnostic = f"{diagnostic}:vendor_code={original_args[0]}"
                if original_args[0] == 1054 and len(original_args) > 1:
                    unknown_column_detail = str(original_args[1])[:300]
                    match = re.search(r"Unknown column '([^']+)'", unknown_column_detail)
                    if match:
                        diagnostic = f"{diagnostic}:column={match.group(1)}"
                    else:
                        diagnostic = f"{diagnostic}:detail={unknown_column_detail}"
        statement = re.sub(r"\s+", " ", str(getattr(exc, "statement", "") or "")).strip()
        if statement:
            diagnostic = f"{diagnostic}:statement={statement[:1000]}"
        frames = traceback.extract_tb(exc.__traceback__)[-6:]
        if frames:
            diagnostic = f"{diagnostic}:frames=" + ">".join(
                f"{frame.name}@{frame.lineno}" for frame in frames
            )
        await session.rollback()
        recovered_job = await session.scalar(
            select(JobRunLog)
            .where(JobRunLog.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if recovered_job is None:
            raise RuntimeError("JOB_NOT_FOUND_AFTER_ROLLBACK") from exc
        job = recovered_job
        if (
            job.status != "running"
            or job.locked_by != worker_instance
            or job.fencing_token != expected_fencing_token
        ):
            ownership_lost = True
            raise JobOwnershipLost("JOB_OWNERSHIP_LOST") from exc
        retryable = _job_error_is_retryable(error_code)
        if retryable and job.attempt_count < job.max_attempts:
            job.status = "retry_wait"
            job.next_run_at = utcnow() + _job_retry_delay(job.job_type, job.attempt_count)
        else:
            job.status = "needs_manual_review" if error_code.startswith("SMTP_") else "failed"
            job.finished_at = utcnow()
        job.failed_count += 1
        job.error_code = error_code
        job.error_message = diagnostic
    finally:
        if not ownership_lost:
            job.duration_ms = int((utcnow() - started).total_seconds() * 1000)
            if job.status == "success":
                job.finished_at = utcnow()
            job.locked_at = None
            job.locked_by = None
            job.fencing_token = None
            job.execution_deadline_at = None
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
            if job.status == "success":
                log = logger.info
                runtime_event = "job_completed"
            elif job.status == "retry_wait":
                log = logger.warning
                runtime_event = "job_retrying"
            elif job.status == "queued":
                log = logger.info
                runtime_event = "job_yielded"
            else:
                log = logger.error
                runtime_event = "job_failed"
            log(
                "Background job execution completed",
                extra={
                    "event": runtime_event,
                    "job_run_id": job.id,
                    "job_type": job.job_type,
                    "resource_type": job.resource_type,
                    "resource_id": job.resource_id,
                    "attempt": job.attempt_count,
                    "max_attempt": job.max_attempts,
                    "next_retry_at": job.next_run_at.isoformat() if job.next_run_at else None,
                    "worker_instance": worker_instance,
                    "status": job.status,
                    "duration_ms": job.duration_ms,
                    "error_code": job.error_code,
                },
            )
    return job
