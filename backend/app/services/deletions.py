from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from jose import JWTError, jwt
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    AiCallLog,
    Email,
    EmailAttachment,
    EmailThread,
    EmailTicketLink,
    ExportSap,
    ExternalOperationRecord,
    FieldAuditLog,
    JobRunLog,
    MailFetchRecord,
    ManualReviewTask,
    NotificationEvent,
    NotificationUserState,
    OperationLog,
    OssObject,
    ParseResult,
    RepairTicket,
    RepairTicketItem,
    ReplyRecord,
    SnValidationResult,
    SystemEventLog,
    TicketRelayExport,
    TicketRma,
    TicketRmaItem,
    TicketStatusLog,
)
from app.services.audit import log_operation
from app.services.common import model_to_dict, sha256_text, utcnow
from app.services.external_operations import (
    fail_external_operation,
    start_external_operation,
    succeed_external_operation,
)
from app.services.jobs import enqueue_job
from app.services.mail_safety import test_envelope_allowed
from app.services.storage import (
    StorageConfigurationError,
    StorageDeleteError,
    delete_oss_object,
    oss_reference_summary,
)


ACTIVE_JOB_STATUSES = {"queued", "running", "retry_wait"}
RUNNING_JOB_STATUSES = {"running"}
OPEN_TASK_STATUSES = {"pending", "assigned", "claimed", "assignment_failed"}
IRREVERSIBLE_REPLY_STATUSES = {"sent", "send_uncertain"}
IRREVERSIBLE_EXTERNAL_STATUSES = {"succeeded", "uncertain"}
IRREVERSIBLE_EXTERNAL_TYPES = {"smtp_send", "relay_insert", "relay_insert_reconcile"}
IRREVERSIBLE_EXPORT_STATUSES = {
    "submitted",
    "accepted",
    "waiting_sap_result",
    "waiting_rma",
    "rma_received",
    "submit_unknown",
}
IRREVERSIBLE_RMA_STATUSES = {"sent", "issued"}
DELETE_PREVIEW_TTL_MINUTES = 10


class DeletionError(RuntimeError):
    def __init__(self, code: str, *, status_code: int, data: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.data = data or {}


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _token(resource_type: str, resource_id: int, preview_hash: str, user_id: int) -> str:
    now = utcnow()
    return jwt.encode(
        {
            "typ": "delete_preview",
            "sub": str(user_id),
            "resource_type": resource_type,
            "resource_id": resource_id,
            "preview_hash": preview_hash,
            "iat": int(now.timestamp()),
            "exp": now + timedelta(minutes=DELETE_PREVIEW_TTL_MINUTES),
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def _validate_token(token: str, *, resource_type: str, resource_id: int, preview_hash: str, user_id: int) -> None:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise DeletionError("DELETE_CONFIRMATION_INVALID", status_code=409) from exc
    if (
        payload.get("typ") != "delete_preview"
        or payload.get("sub") != str(user_id)
        or payload.get("resource_type") != resource_type
        or int(payload.get("resource_id") or 0) != resource_id
        or payload.get("preview_hash") != preview_hash
    ):
        raise DeletionError("DELETE_PREVIEW_STALE", status_code=409)


async def _ids(session: AsyncSession, statement) -> list[int]:
    return [int(value) for value in (await session.execute(statement)).scalars().all()]


async def _count(session: AsyncSession, model, *conditions) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)


async def _oss_preview(session: AsyncSession, object_ids: list[int]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for object_id in sorted(set(value for value in object_ids if value)):
        obj = await session.get(OssObject, object_id)
        if obj is None:
            continue
        refs = await oss_reference_summary(session, obj.id)
        result.append(
            {
                "id": obj.id,
                "bucket": obj.bucket,
                "object_key_hash": sha256_text(obj.object_key),
                "sha256": obj.sha256_hash,
                "reference_counts": refs,
                "reference_total": sum(refs.values()),
            }
        )
    return result


def _finish_preview(data: dict[str, Any], user_id: int) -> dict[str, Any]:
    preview_hash = _canonical_hash(data)
    return {
        **data,
        "preview_hash": preview_hash,
        "confirmation_token": _token(data["resource_type"], data["resource_id"], preview_hash, user_id),
        "confirmation_expires_minutes": DELETE_PREVIEW_TTL_MINUTES,
    }


async def preview_attachment(session: AsyncSession, attachment_id: int, user_id: int) -> dict[str, Any]:
    attachment = await session.get(EmailAttachment, attachment_id)
    if attachment is None:
        raise DeletionError("ATTACHMENT_NOT_FOUND", status_code=404)
    parent_email = await session.get(Email, attachment.email_id)
    parse_ids = await _ids(session, select(ParseResult.id).where(ParseResult.source_attachment_id == attachment.id))
    applied_count = await _count(
        session,
        ParseResult,
        ParseResult.source_attachment_id == attachment.id,
        or_(ParseResult.accepted.is_(True), ParseResult.applied_at.is_not(None), ParseResult.apply_status == "applied"),
    )
    field_refs = await _count(session, FieldAuditLog, FieldAuditLog.parse_result_id.in_(parse_ids or [-1]))
    ai_ids = await _ids(session, select(AiCallLog.id).where(AiCallLog.attachment_id == attachment.id))
    reply_ai_refs = await _count(session, ReplyRecord, ReplyRecord.ai_call_log_id.in_(ai_ids or [-1]))
    source_refs = await _count(session, RepairTicket, RepairTicket.source_email_id == attachment.email_id)
    critical_link_refs = await _count(
        session,
        EmailTicketLink,
        EmailTicketLink.email_id == attachment.email_id,
        EmailTicketLink.link_type.in_(["source", "outbound"]),
    )
    sent_outgoing_refs = await _count(
        session,
        ReplyRecord,
        ReplyRecord.outgoing_email_id == attachment.email_id,
        ReplyRecord.send_status.in_(IRREVERSIBLE_REPLY_STATUSES),
    )
    reply_pdf_refs = 0
    rma_pdf_refs = 0
    if attachment.oss_object_id:
        reply_pdf_refs = await _count(
            session, ReplyRecord, ReplyRecord.rma_pdf_oss_object_id == attachment.oss_object_id
        )
        rma_pdf_refs = await _count(
            session, TicketRma, TicketRma.pdf_oss_object_id == attachment.oss_object_id
        )
    active_jobs = await _ids(
        session,
        select(JobRunLog.id).where(
            JobRunLog.resource_type == "email",
            JobRunLog.resource_id == attachment.email_id,
            JobRunLog.status.in_(ACTIVE_JOB_STATUSES),
        ),
    )
    blockers: list[str] = []
    if applied_count or field_refs:
        blockers.append("ATTACHMENT_PARSE_RESULT_APPLIED")
    if reply_ai_refs:
        blockers.append("ATTACHMENT_AI_LOG_IN_USE")
    if source_refs or critical_link_refs:
        blockers.append("ATTACHMENT_PARENT_EMAIL_IS_BUSINESS_EVIDENCE")
    if sent_outgoing_refs or (parent_email is not None and parent_email.mail_direction == "outbound" and parent_email.parse_status == "sent"):
        blockers.append("ATTACHMENT_PARENT_EMAIL_ALREADY_SENT")
    if reply_pdf_refs or rma_pdf_refs:
        blockers.append("ATTACHMENT_FORMAL_DOCUMENT_IN_USE")
    running_jobs = await _count(
        session,
        JobRunLog,
        JobRunLog.resource_type == "email",
        JobRunLog.resource_id == attachment.email_id,
        JobRunLog.status.in_(RUNNING_JOB_STATUSES),
    )
    if running_jobs:
        blockers.append("ATTACHMENT_IN_ACTIVE_JOB")
    data = {
        "resource_type": "attachment",
        "resource_id": attachment.id,
        "resource_version": attachment.created_at.isoformat() if attachment.created_at else None,
        "parent_email_id": attachment.email_id,
        "affected_counts": {
            "parse_results": len(parse_ids),
            "ai_call_logs": len(ai_ids),
            "reply_pdf_references": reply_pdf_refs,
            "rma_pdf_references": rma_pdf_refs,
        },
        "active_job_ids": active_jobs,
        "blockers": blockers,
        "oss_objects": await _oss_preview(session, [attachment.oss_object_id] if attachment.oss_object_id else []),
        "deletable": not blockers,
    }
    return _finish_preview(data, user_id)


async def preview_email(session: AsyncSession, email_id: int, user_id: int) -> dict[str, Any]:
    email = await session.get(Email, email_id)
    if email is None:
        raise DeletionError("EMAIL_NOT_FOUND", status_code=404)
    attachment_rows = list((await session.execute(select(EmailAttachment).where(EmailAttachment.email_id == email.id))).scalars().all())
    attachment_ids = [row.id for row in attachment_rows]
    ai_ids = await _ids(
        session,
        select(AiCallLog.id).where(
            or_(
                AiCallLog.email_id == email.id,
                AiCallLog.attachment_id.in_(attachment_ids or [-1]),
            )
        ),
    )
    ai_reply_refs = await _count(
        session, ReplyRecord, ReplyRecord.ai_call_log_id.in_(ai_ids or [-1])
    )
    parse_rows = list((await session.execute(select(ParseResult).where(ParseResult.email_id == email.id))).scalars().all())
    link_rows = list((await session.execute(select(EmailTicketLink).where(EmailTicketLink.email_id == email.id))).scalars().all())
    reply_rows = list(
        (
            await session.execute(
                select(ReplyRecord).where(
                    or_(ReplyRecord.related_email_id == email.id, ReplyRecord.outgoing_email_id == email.id)
                )
            )
        ).scalars().all()
    )
    source_count = await _count(session, RepairTicket, RepairTicket.source_email_id == email.id)
    open_tasks = await _ids(
        session,
        select(ManualReviewTask.id).where(
            ManualReviewTask.email_id == email.id,
            ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
        ),
    )
    active_jobs = await _ids(
        session,
        select(JobRunLog.id).where(
            JobRunLog.resource_type == "email",
            JobRunLog.resource_id == email.id,
            JobRunLog.status.in_(ACTIVE_JOB_STATUSES),
        ),
    )
    applied_parse = any(row.accepted or row.applied_at or row.apply_status == "applied" for row in parse_rows)
    critical_links = [row.link_type for row in link_rows if row.link_type in {"source", "outbound"}]
    irreversible_replies = [row.id for row in reply_rows if row.send_status in IRREVERSIBLE_REPLY_STATUSES]
    blockers: list[str] = []
    if source_count:
        blockers.append("EMAIL_IS_TICKET_SOURCE")
    if critical_links:
        blockers.append("EMAIL_HAS_CRITICAL_TICKET_LINK")
    if applied_parse:
        blockers.append("EMAIL_PARSE_RESULT_APPLIED")
    if irreversible_replies:
        blockers.append("EMAIL_REPLY_ALREADY_SENT")
    if ai_reply_refs:
        blockers.append("EMAIL_AI_LOG_IN_USE")
    if open_tasks:
        blockers.append("EMAIL_HAS_OPEN_MANUAL_TASK")
    running_jobs = await _count(
        session,
        JobRunLog,
        JobRunLog.resource_type == "email",
        JobRunLog.resource_id == email.id,
        JobRunLog.status.in_(RUNNING_JOB_STATUSES),
    )
    if running_jobs:
        blockers.append("EMAIL_IN_ACTIVE_JOB")
    object_ids = [email.raw_eml_oss_object_id] + [row.oss_object_id for row in attachment_rows]
    data = {
        "resource_type": "email",
        "resource_id": email.id,
        "resource_version": email.updated_at.isoformat() if email.updated_at else None,
        "message_id_hash": sha256_text(email.message_id),
        "thread_id": email.thread_id,
        "affected_counts": {
            "attachments": len(attachment_rows),
            "parse_results": len(parse_rows),
            "ticket_links": len(link_rows),
            "reply_references": len(reply_rows),
            "manual_tasks": await _count(session, ManualReviewTask, ManualReviewTask.email_id == email.id),
            "ai_call_logs": len(ai_ids),
            "ai_log_reply_references": ai_reply_refs,
        },
        "linked_ticket_ids": sorted({row.ticket_id for row in link_rows}),
        "active_job_ids": active_jobs,
        "test_external_envelope_ok": all(
            test_envelope_allowed(row.to_addresses, row.cc_addresses)
            for row in reply_rows
            if row.send_status in IRREVERSIBLE_REPLY_STATUSES
        ),
        "blockers": blockers,
        "oss_objects": await _oss_preview(session, [value for value in object_ids if value]),
        "deletable": not blockers,
    }
    return _finish_preview(data, user_id)


async def _ticket_job_ids(session: AsyncSession, ticket_id: int) -> list[int]:
    reply_ids = await _ids(session, select(ReplyRecord.id).where(ReplyRecord.ticket_id == ticket_id))
    export_ids = await _ids(session, select(TicketRelayExport.id).where(TicketRelayExport.ticket_id == ticket_id))
    return await _ids(
        session,
        select(JobRunLog.id).where(
            JobRunLog.status.in_(ACTIVE_JOB_STATUSES),
            or_(
                (JobRunLog.resource_type == "repair_ticket") & (JobRunLog.resource_id == ticket_id),
                (JobRunLog.resource_type == "reply_record") & JobRunLog.resource_id.in_(reply_ids or [-1]),
                (JobRunLog.resource_type == "ticket_relay_export") & JobRunLog.resource_id.in_(export_ids or [-1]),
            ),
        ),
    )


async def _ticket_running_job_ids(session: AsyncSession, ticket_id: int) -> list[int]:
    reply_ids = await _ids(session, select(ReplyRecord.id).where(ReplyRecord.ticket_id == ticket_id))
    export_ids = await _ids(session, select(TicketRelayExport.id).where(TicketRelayExport.ticket_id == ticket_id))
    return await _ids(
        session,
        select(JobRunLog.id).where(
            JobRunLog.status.in_(RUNNING_JOB_STATUSES),
            or_(
                (JobRunLog.resource_type == "repair_ticket") & (JobRunLog.resource_id == ticket_id),
                (JobRunLog.resource_type == "reply_record") & JobRunLog.resource_id.in_(reply_ids or [-1]),
                (JobRunLog.resource_type == "ticket_relay_export") & JobRunLog.resource_id.in_(export_ids or [-1]),
            ),
        ),
    )


async def preview_ticket(session: AsyncSession, ticket_id: int, user_id: int) -> dict[str, Any]:
    ticket = await session.get(RepairTicket, ticket_id)
    if ticket is None:
        raise DeletionError("TICKET_NOT_FOUND", status_code=404)
    replies = list((await session.execute(select(ReplyRecord).where(ReplyRecord.ticket_id == ticket.id))).scalars().all())
    exports = list((await session.execute(select(ExportSap).where(ExportSap.ticket_id == ticket.id))).scalars().all())
    rmas = list((await session.execute(select(TicketRma).where(TicketRma.ticket_id == ticket.id))).scalars().all())
    external = list((await session.execute(select(ExternalOperationRecord).where(ExternalOperationRecord.ticket_id == ticket.id))).scalars().all())
    active_jobs = await _ticket_job_ids(session, ticket.id)
    running_jobs = await _ticket_running_job_ids(session, ticket.id)
    irreversible = {
        "reply_ids": [row.id for row in replies if row.send_status in IRREVERSIBLE_REPLY_STATUSES],
        "export_ids": [row.id for row in exports if row.remote_call_id or row.status in IRREVERSIBLE_EXPORT_STATUSES],
        "rma_ids": [row.id for row in rmas if row.status in IRREVERSIBLE_RMA_STATUSES or row.sent_at],
        "external_operation_ids": [
            row.id
            for row in external
            if row.operation_type in IRREVERSIBLE_EXTERNAL_TYPES and row.status in IRREVERSIBLE_EXTERNAL_STATUSES
        ],
    }
    uncertain_ids = [row.id for row in external if row.status in {"running", "uncertain"}]
    blockers: list[str] = []
    if running_jobs:
        blockers.append("TICKET_IN_ACTIVE_JOB")
    if uncertain_ids:
        blockers.append("TICKET_EXTERNAL_OPERATION_ACTIVE_OR_UNCERTAIN")
    if any(irreversible.values()) or ticket.current_status_code in {"rma_sent", "closed"}:
        blockers.append("TICKET_EXTERNAL_EFFECT_ALREADY_EXECUTED")
    object_ids = [row.rma_pdf_oss_object_id for row in replies] + [row.pdf_oss_object_id for row in rmas]
    data = {
        "resource_type": "ticket",
        "resource_id": ticket.id,
        "resource_version": ticket.version,
        "ticket_no": ticket.ticket_no,
        "status": ticket.current_status_code,
        "affected_counts": {
            "items": await _count(session, RepairTicketItem, RepairTicketItem.ticket_id == ticket.id),
            "email_links": await _count(session, EmailTicketLink, EmailTicketLink.ticket_id == ticket.id),
            "parse_results_detached": await _count(session, ParseResult, ParseResult.ticket_id == ticket.id),
            "manual_tasks": await _count(session, ManualReviewTask, ManualReviewTask.ticket_id == ticket.id),
            "replies": len(replies),
            "rmas": len(rmas),
            "sap_exports": len(exports),
            "external_operations": len(external),
        },
        "active_job_ids": active_jobs,
        "running_job_ids": running_jobs,
        "test_external_envelope_ok": all(
            test_envelope_allowed(row.to_addresses, row.cc_addresses)
            for row in replies
            if row.send_status in IRREVERSIBLE_REPLY_STATUSES
        ),
        "irreversible_effects": irreversible,
        "uncertain_operation_ids": uncertain_ids,
        "blockers": blockers,
        "oss_objects": await _oss_preview(session, [value for value in object_ids if value]),
        "deletable": not blockers,
        "force_local_cleanup_available": bool(blockers == ["TICKET_EXTERNAL_EFFECT_ALREADY_EXECUTED"]),
    }
    return _finish_preview(data, user_id)


async def _prepare_oss_operations(
    session: AsyncSession,
    *,
    audit: OperationLog,
    object_ids: list[int],
    removed_reference_counts: dict[int, int],
) -> tuple[list[int], list[int]]:
    operation_ids: list[int] = []
    shared_ids: list[int] = []
    for object_id in sorted(set(value for value in object_ids if value)):
        obj = await session.get(OssObject, object_id)
        if obj is None:
            continue
        refs = await oss_reference_summary(session, obj.id)
        remaining = sum(refs.values()) - int(removed_reference_counts.get(obj.id, 0))
        if remaining > 0:
            shared_ids.append(obj.id)
            continue
        operation = await start_external_operation(
            session,
            operation_type="oss_delete",
            operation_key=f"delete:{audit.id}:{obj.id}:{sha256_text(obj.object_key)[:20]}",
            recovery_stage="oss_delete_pending",
            details={
                "deletion_audit_id": audit.id,
                "oss_object_id": obj.id,
                "bucket": obj.bucket,
                "endpoint": obj.endpoint,
                "object_key": obj.object_key,
                "object_version": obj.object_version,
                "sha256": obj.sha256_hash,
            },
        )
        operation.status = "failed_retryable"
        operation.error_code = "OSS_DELETE_PENDING"
        operation.retryable = True
        operation_ids.append(operation.id)
    return operation_ids, shared_ids


async def _create_delete_audit(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_id: int,
    user_id: int,
    reason: str,
    preview: dict[str, Any],
    force_local_cleanup: bool,
) -> OperationLog:
    audit = await log_operation(
        session,
        operation_type=f"{resource_type}_deleted",
        target_type=resource_type,
        target_id=resource_id,
        user_id=user_id,
        description=reason,
        before_data={
            "preview_hash": preview["preview_hash"],
            "affected_counts": preview.get("affected_counts"),
            "blockers": preview.get("blockers"),
            "force_local_cleanup": force_local_cleanup,
            "external_effects": preview.get("irreversible_effects"),
        },
        after_data={"database_status": "planned", "oss_status": "pending"},
    )
    await session.flush()
    return audit


async def _ensure_force_allowed(session: AsyncSession, preview: dict[str, Any], force_local_cleanup: bool) -> None:
    blockers = set(preview.get("blockers") or [])
    if not blockers:
        return
    if not force_local_cleanup:
        raise DeletionError("DELETE_BLOCKED", status_code=409, data={"blockers": sorted(blockers)})
    database_name = str(await session.scalar(select(func.database())) or "")
    allowed_databases = {name.strip() for name in settings.DESTRUCTIVE_TEST_DATABASE_ALLOWLIST if name.strip()}
    if (
        settings.APP_ENV.strip().lower() not in {"dev", "test"}
        or database_name != settings.database_name
        or database_name not in allowed_databases
    ):
        raise DeletionError("LOCAL_FORCE_DELETE_NOT_ALLOWED", status_code=403)
    non_overridable = blockers - {"TICKET_EXTERNAL_EFFECT_ALREADY_EXECUTED", "EMAIL_REPLY_ALREADY_SENT"}
    if non_overridable:
        raise DeletionError("DELETE_BLOCKED", status_code=409, data={"blockers": sorted(non_overridable)})
    if not preview.get("test_external_envelope_ok", True):
        raise DeletionError("TEST_MAIL_ENVELOPE_REQUIRED_FOR_FORCE_DELETE", status_code=403)
    if preview["resource_type"] == "ticket" and preview.get("irreversible_effects", {}).get("export_ids"):
        if settings.RELAY_ADAPTER != "test_http":
            raise DeletionError("TEST_RELAY_REQUIRED_FOR_FORCE_DELETE", status_code=403)


async def _finalize_database_delete(
    session: AsyncSession,
    *,
    audit: OperationLog,
    operation_ids: list[int],
    shared_ids: list[int],
    affected_counts: dict[str, Any],
    user_id: int,
) -> JobRunLog | None:
    job: JobRunLog | None = None
    if operation_ids:
        job = await enqueue_job(
            session,
            job_type="oss_delete",
            resource_type="operation_log",
            resource_id=audit.id,
            idempotency_key=f"oss_delete:{audit.id}",
            metadata={"user_id": user_id, "external_operation_ids": operation_ids},
            max_attempts=5,
            correlation_id=audit.correlation_id,
        )
    audit.after_data = {
        "database_status": "deleted",
        "oss_status": "pending" if operation_ids else "completed",
        "affected_counts": affected_counts,
        "shared_oss_object_ids": shared_ids,
        "oss_operation_ids": operation_ids,
        "job_id": job.id if job else None,
    }
    return job


async def process_oss_deletion_operation(session: AsyncSession, audit_log_id: int) -> dict[str, Any]:
    audit = await session.get(OperationLog, audit_log_id, with_for_update=True)
    if audit is None or audit.operation_type not in {"attachment_deleted", "email_deleted", "ticket_deleted", "gold_test_replay_reset"}:
        raise DeletionError("DELETION_OPERATION_NOT_FOUND", status_code=404)
    operations = list(
        (
            await session.execute(
                select(ExternalOperationRecord).where(
                    ExternalOperationRecord.operation_type == "oss_delete",
                    ExternalOperationRecord.operation_key.like(f"delete:{audit.id}:%"),
                )
            )
        ).scalars().all()
    )
    failed = 0
    completed = 0
    job = await session.scalar(
        select(JobRunLog)
        .where(JobRunLog.resource_type == "operation_log", JobRunLog.resource_id == audit.id)
        .order_by(JobRunLog.id.desc())
    )
    worker_managed_job = bool(job is not None and job.status == "running" and job.locked_by)
    if job is not None and job.status in {"queued", "retry_wait"}:
        job.status = "running"
        job.started_at = job.started_at or utcnow()
        job.attempt_count = int(job.attempt_count or 0) + 1
    for operation in operations:
        if operation.status == "succeeded":
            completed += 1
            continue
        details = operation.details_json or {}
        operation.status = "running"
        operation.attempt_count = int(operation.attempt_count or 0) + 1
        try:
            await delete_oss_object(
                bucket=str(details["bucket"]),
                object_key=str(details["object_key"]),
                endpoint=details.get("endpoint"),
                object_version=details.get("object_version"),
            )
            object_id = int(details.get("oss_object_id") or 0)
            if object_id:
                refs = await oss_reference_summary(session, object_id)
                if sum(refs.values()) == 0:
                    obj = await session.get(OssObject, object_id)
                    if obj is not None:
                        await session.delete(obj)
            succeed_external_operation(operation)
            completed += 1
        except StorageConfigurationError as exc:
            fail_external_operation(
                operation,
                error_code="OSS_NOT_CONFIGURED",
                error_message=str(exc),
                retryable=True,
                recovery_stage="oss_delete_retry",
                next_retry_at=utcnow() + timedelta(minutes=5),
            )
            failed += 1
        except StorageDeleteError as exc:
            fail_external_operation(
                operation,
                error_code=exc.code,
                error_message=exc.code,
                retryable=exc.retryable,
                recovery_stage="oss_delete_retry" if exc.retryable else "oss_delete_manual",
                next_retry_at=utcnow() + timedelta(minutes=5) if exc.retryable else None,
            )
            failed += 1
    after = dict(audit.after_data or {})
    after["oss_status"] = "completed" if failed == 0 else "retry_wait"
    after["oss_completed_count"] = completed
    after["oss_failed_count"] = failed
    audit.after_data = after
    if job is not None and not worker_managed_job:
        if failed:
            job.status = "retry_wait"
            job.error_code = "OSS_DELETE_PENDING"
            job.next_run_at = utcnow() + timedelta(minutes=5)
            job.failed_count = int(job.failed_count or 0) + failed
        else:
            job.status = "success"
            job.error_code = None
            job.next_run_at = None
            job.finished_at = utcnow()
            job.success_count = max(1, completed)
    return {
        "status": "success" if failed == 0 else "failed",
        "audit_log_id": audit.id,
        "completed_count": completed,
        "failed_count": failed,
        "success_count": completed,
        "error_code": "OSS_DELETE_PENDING" if failed else None,
    }


async def delete_attachment(
    session: AsyncSession,
    *,
    attachment_id: int,
    user_id: int,
    reason: str,
    confirmation_token: str,
) -> dict[str, Any]:
    attachment = await session.get(EmailAttachment, attachment_id, with_for_update=True)
    if attachment is None:
        raise DeletionError("ATTACHMENT_NOT_FOUND", status_code=404)
    preview = await preview_attachment(session, attachment_id, user_id)
    _validate_token(confirmation_token, resource_type="attachment", resource_id=attachment_id, preview_hash=preview["preview_hash"], user_id=user_id)
    await _ensure_force_allowed(session, preview, False)
    audit = await _create_delete_audit(session, resource_type="attachment", resource_id=attachment.id, user_id=user_id, reason=reason, preview=preview, force_local_cleanup=False)
    await session.execute(
        update(JobRunLog)
        .where(JobRunLog.id.in_(preview.get("active_job_ids") or [-1]), JobRunLog.status.in_(["queued", "retry_wait"]))
        .values(status="superseded", error_code="RESOURCE_DELETED", next_run_at=None, finished_at=utcnow())
    )
    object_ids = [attachment.oss_object_id] if attachment.oss_object_id else []
    operation_ids, shared_ids = await _prepare_oss_operations(
        session,
        audit=audit,
        object_ids=object_ids,
        removed_reference_counts={attachment.oss_object_id: 1} if attachment.oss_object_id else {},
    )
    parse_ids = await _ids(session, select(ParseResult.id).where(ParseResult.source_attachment_id == attachment.id))
    ai_ids = await _ids(session, select(AiCallLog.id).where(AiCallLog.attachment_id == attachment.id))
    await session.execute(delete(FieldAuditLog).where(FieldAuditLog.parse_result_id.in_(parse_ids or [-1])))
    await session.execute(delete(ParseResult).where(ParseResult.id.in_(parse_ids or [-1])))
    await session.execute(delete(AiCallLog).where(AiCallLog.id.in_(ai_ids or [-1])))
    await session.delete(attachment)
    counts = {"email_attachments": 1, "parse_results": len(parse_ids), "ai_call_logs": len(ai_ids)}
    job = await _finalize_database_delete(session, audit=audit, operation_ids=operation_ids, shared_ids=shared_ids, affected_counts=counts, user_id=user_id)
    await session.commit()
    oss_result = await process_oss_deletion_operation(session, audit.id) if operation_ids else {"failed_count": 0}
    await session.commit()
    return {"deleted": True, "deletion_operation_id": audit.id, "resource_type": "attachment", "resource_id": attachment_id, "database_status": "deleted", "oss_status": "completed" if not oss_result.get("failed_count") else "pending", "job_id": job.id if job else None, "affected_row_counts": counts}


async def delete_email(
    session: AsyncSession,
    *, email_id: int,
    user_id: int,
    reason: str,
    confirmation_token: str,
    force_local_cleanup: bool = False,
) -> dict[str, Any]:
    email = await session.get(Email, email_id, with_for_update=True)
    if email is None:
        raise DeletionError("EMAIL_NOT_FOUND", status_code=404)
    preview = await preview_email(session, email_id, user_id)
    _validate_token(confirmation_token, resource_type="email", resource_id=email_id, preview_hash=preview["preview_hash"], user_id=user_id)
    await _ensure_force_allowed(session, preview, force_local_cleanup)
    audit = await _create_delete_audit(session, resource_type="email", resource_id=email.id, user_id=user_id, reason=reason, preview=preview, force_local_cleanup=force_local_cleanup)
    await session.execute(
        update(JobRunLog)
        .where(JobRunLog.id.in_(preview.get("active_job_ids") or [-1]), JobRunLog.status.in_(["queued", "retry_wait"]))
        .values(status="superseded", error_code="RESOURCE_DELETED", next_run_at=None, finished_at=utcnow())
    )
    attachments = list((await session.execute(select(EmailAttachment).where(EmailAttachment.email_id == email.id))).scalars().all())
    object_ids = [email.raw_eml_oss_object_id] + [row.oss_object_id for row in attachments]
    removed: dict[int, int] = {}
    for object_id in [value for value in object_ids if value]:
        removed[object_id] = removed.get(object_id, 0) + 1
    operation_ids, shared_ids = await _prepare_oss_operations(session, audit=audit, object_ids=[value for value in object_ids if value], removed_reference_counts=removed)
    parse_ids = await _ids(session, select(ParseResult.id).where(ParseResult.email_id == email.id))
    attachment_ids = [row.id for row in attachments]
    ai_ids = await _ids(
        session,
        select(AiCallLog.id).where(
            or_(
                AiCallLog.email_id == email.id,
                AiCallLog.attachment_id.in_(attachment_ids or [-1]),
            )
        ),
    )
    await session.execute(update(ReplyRecord).where(ReplyRecord.ai_call_log_id.in_(ai_ids or [-1])).values(ai_call_log_id=None))
    await session.execute(update(ReplyRecord).where(or_(ReplyRecord.related_email_id == email.id, ReplyRecord.outgoing_email_id == email.id)).values(related_email_id=None, outgoing_email_id=None))
    await session.execute(delete(FieldAuditLog).where(FieldAuditLog.parse_result_id.in_(parse_ids or [-1])))
    await session.execute(delete(ParseResult).where(ParseResult.id.in_(parse_ids or [-1])))
    await session.execute(delete(AiCallLog).where(or_(AiCallLog.email_id == email.id, AiCallLog.attachment_id.in_(attachment_ids or [-1]))))
    await session.execute(delete(EmailTicketLink).where(EmailTicketLink.email_id == email.id))
    await session.execute(update(ManualReviewTask).where(ManualReviewTask.email_id == email.id).values(email_id=None))
    await session.execute(update(MailFetchRecord).where(MailFetchRecord.email_id == email.id).values(email_id=None, duplicate=True, fetch_status="deleted"))
    await session.execute(update(ExternalOperationRecord).where(ExternalOperationRecord.email_id == email.id).values(email_id=None))
    await session.execute(update(OperationLog).where(OperationLog.email_id == email.id).values(email_id=None))
    await session.execute(update(SystemEventLog).where(SystemEventLog.email_id == email.id).values(email_id=None))
    await session.execute(update(Email).where(Email.duplicate_of_email_id == email.id).values(duplicate_of_email_id=None))
    if email.thread_id:
        await session.execute(update(EmailThread).where(EmailThread.id == email.thread_id, EmailThread.latest_email_id == email.id).values(latest_email_id=None))
    await session.execute(delete(EmailAttachment).where(EmailAttachment.email_id == email.id))
    thread_id = email.thread_id
    await session.delete(email)
    await session.flush()
    if thread_id:
        thread = await session.get(EmailThread, thread_id, with_for_update=True)
        if thread is not None:
            remaining = list((await session.execute(select(Email).where(Email.thread_id == thread.id).order_by(Email.received_at.asc(), Email.id.asc()))).scalars().all())
            if remaining:
                thread.root_message_id = remaining[0].message_id
                thread.latest_email_id = remaining[-1].id
                thread.email_count = len(remaining)
                thread.thread_version = int(thread.thread_version or 0) + 1
            else:
                ticket_refs = await _count(session, RepairTicket, RepairTicket.thread_id == thread.id)
                successor_refs = await _count(session, EmailThread, EmailThread.predecessor_thread_id == thread.id)
                if not ticket_refs and not successor_refs and thread.ticket_id is None and thread.predecessor_ticket_id is None:
                    await session.delete(thread)
                else:
                    thread.root_message_id = None
                    thread.latest_email_id = None
                    thread.email_count = 0
                    thread.thread_version = int(thread.thread_version or 0) + 1
    counts = {"emails": 1, "email_attachments": len(attachments), "parse_results": len(parse_ids), "ai_call_logs": len(ai_ids)}
    job = await _finalize_database_delete(session, audit=audit, operation_ids=operation_ids, shared_ids=shared_ids, affected_counts=counts, user_id=user_id)
    await session.commit()
    oss_result = await process_oss_deletion_operation(session, audit.id) if operation_ids else {"failed_count": 0}
    await session.commit()
    return {"deleted": True, "deletion_operation_id": audit.id, "resource_type": "email", "resource_id": email_id, "database_status": "deleted", "oss_status": "completed" if not oss_result.get("failed_count") else "pending", "job_id": job.id if job else None, "affected_row_counts": counts, "external_effects_not_reverted": force_local_cleanup}


async def delete_ticket(
    session: AsyncSession,
    *, ticket_id: int,
    user_id: int,
    reason: str,
    confirmation_token: str,
    force_local_cleanup: bool = False,
) -> dict[str, Any]:
    ticket = await session.get(RepairTicket, ticket_id, with_for_update=True)
    if ticket is None:
        raise DeletionError("TICKET_NOT_FOUND", status_code=404)
    preview = await preview_ticket(session, ticket_id, user_id)
    _validate_token(confirmation_token, resource_type="ticket", resource_id=ticket_id, preview_hash=preview["preview_hash"], user_id=user_id)
    await _ensure_force_allowed(session, preview, force_local_cleanup)
    audit = await _create_delete_audit(session, resource_type="ticket", resource_id=ticket.id, user_id=user_id, reason=reason, preview=preview, force_local_cleanup=force_local_cleanup)
    replies = list((await session.execute(select(ReplyRecord).where(ReplyRecord.ticket_id == ticket.id))).scalars().all())
    rmas = list((await session.execute(select(TicketRma).where(TicketRma.ticket_id == ticket.id))).scalars().all())
    object_ids = [row.rma_pdf_oss_object_id for row in replies] + [row.pdf_oss_object_id for row in rmas]
    removed: dict[int, int] = {}
    for object_id in [value for value in object_ids if value]:
        removed[object_id] = removed.get(object_id, 0) + 1
    operation_ids, shared_ids = await _prepare_oss_operations(session, audit=audit, object_ids=[value for value in object_ids if value], removed_reference_counts=removed)
    item_ids = await _ids(session, select(RepairTicketItem.id).where(RepairTicketItem.ticket_id == ticket.id))
    reply_ids = [row.id for row in replies]
    rma_ids = [row.id for row in rmas]
    export_ids = await _ids(session, select(TicketRelayExport.id).where(TicketRelayExport.ticket_id == ticket.id))
    sap_ids = await _ids(session, select(ExportSap.id).where(ExportSap.ticket_id == ticket.id))
    task_ids = await _ids(session, select(ManualReviewTask.id).where(ManualReviewTask.ticket_id == ticket.id))
    notification_ids = await _ids(session, select(NotificationEvent.id).where(or_(NotificationEvent.ticket_id == ticket.id, (NotificationEvent.target_type.in_(["ticket", "repair_ticket"])) & (NotificationEvent.target_id == ticket.id), (NotificationEvent.target_type == "manual_review_task") & NotificationEvent.target_id.in_(task_ids or [-1]))))
    await session.execute(update(JobRunLog).where(JobRunLog.id.in_(preview.get("active_job_ids") or [-1]), JobRunLog.status.in_(["queued", "retry_wait"])).values(status="superseded", error_code="RESOURCE_DELETED", next_run_at=None, finished_at=utcnow()))
    await session.execute(delete(NotificationUserState).where(NotificationUserState.notification_id.in_(notification_ids or [-1])))
    await session.execute(delete(NotificationEvent).where(NotificationEvent.id.in_(notification_ids or [-1])))
    await session.execute(update(EmailThread).where(EmailThread.ticket_id == ticket.id).values(ticket_id=None))
    await session.execute(update(EmailThread).where(EmailThread.predecessor_ticket_id == ticket.id).values(predecessor_ticket_id=None))
    await session.execute(delete(EmailTicketLink).where(EmailTicketLink.ticket_id == ticket.id))
    await session.execute(update(ParseResult).where(ParseResult.ticket_id == ticket.id).values(ticket_id=None))
    await session.execute(
        update(ReplyRecord)
        .where(ReplyRecord.id.in_(reply_ids or [-1]))
        .values(ai_call_log_id=None)
    )
    await session.execute(delete(AiCallLog).where(AiCallLog.ticket_id == ticket.id, AiCallLog.email_id.is_(None)))
    await session.execute(update(AiCallLog).where(AiCallLog.ticket_id == ticket.id).values(ticket_id=None))
    await session.execute(update(OperationLog).where(OperationLog.ticket_id == ticket.id).values(ticket_id=None))
    await session.execute(update(SystemEventLog).where(SystemEventLog.ticket_id == ticket.id).values(ticket_id=None))
    await session.execute(delete(ExternalOperationRecord).where(or_(ExternalOperationRecord.ticket_id == ticket.id, ExternalOperationRecord.reply_record_id.in_(reply_ids or [-1]), ExternalOperationRecord.export_sap_id.in_(sap_ids or [-1]))))
    await session.execute(delete(FieldAuditLog).where(FieldAuditLog.ticket_id == ticket.id))
    await session.execute(delete(TicketStatusLog).where(TicketStatusLog.ticket_id == ticket.id))
    await session.execute(delete(SnValidationResult).where(SnValidationResult.ticket_id == ticket.id))
    await session.execute(delete(ManualReviewTask).where(ManualReviewTask.ticket_id == ticket.id))
    await session.execute(delete(TicketRmaItem).where(TicketRmaItem.ticket_rma_id.in_(rma_ids or [-1])))
    await session.execute(delete(TicketRma).where(TicketRma.id.in_(rma_ids or [-1])))
    await session.execute(delete(ReplyRecord).where(ReplyRecord.id.in_(reply_ids or [-1])))
    await session.execute(delete(ExportSap).where(ExportSap.ticket_id == ticket.id))
    await session.execute(delete(TicketRelayExport).where(TicketRelayExport.id.in_(export_ids or [-1])))
    await session.execute(delete(RepairTicketItem).where(RepairTicketItem.id.in_(item_ids or [-1])))
    await session.delete(ticket)
    counts = dict(preview.get("affected_counts") or {})
    counts["repair_tickets"] = 1
    job = await _finalize_database_delete(session, audit=audit, operation_ids=operation_ids, shared_ids=shared_ids, affected_counts=counts, user_id=user_id)
    await session.commit()
    oss_result = await process_oss_deletion_operation(session, audit.id) if operation_ids else {"failed_count": 0}
    await session.commit()
    return {"deleted": True, "deletion_operation_id": audit.id, "resource_type": "ticket", "resource_id": ticket_id, "database_status": "deleted", "oss_status": "completed" if not oss_result.get("failed_count") else "pending", "job_id": job.id if job else None, "affected_row_counts": counts, "external_effects_not_reverted": force_local_cleanup}


async def get_deletion_operation(session: AsyncSession, audit_log_id: int) -> dict[str, Any]:
    audit = await session.get(OperationLog, audit_log_id)
    if audit is None or audit.operation_type not in {"attachment_deleted", "email_deleted", "ticket_deleted", "gold_test_replay_reset"}:
        raise DeletionError("DELETION_OPERATION_NOT_FOUND", status_code=404)
    operations = list((await session.execute(select(ExternalOperationRecord).where(ExternalOperationRecord.operation_type == "oss_delete", ExternalOperationRecord.operation_key.like(f"delete:{audit.id}:%")))).scalars().all())
    job = await session.scalar(select(JobRunLog).where(JobRunLog.resource_type == "operation_log", JobRunLog.resource_id == audit.id).order_by(JobRunLog.id.desc()))
    return {
        "audit_log_id": audit.id,
        "operation_type": audit.operation_type,
        "target_type": audit.target_type,
        "target_id": audit.target_id,
        "created_at": audit.created_at,
        "result": audit.after_data,
        "job": model_to_dict(job, ("id", "status", "attempt_count", "error_code", "next_run_at")) if job else None,
        "oss": [{"id": row.id, "status": row.status, "attempt_count": row.attempt_count, "error_code": row.error_code, "next_retry_at": row.next_retry_at} for row in operations],
    }
