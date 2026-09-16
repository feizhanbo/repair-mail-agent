from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import JobRunLog


JobHandler = Callable[[AsyncSession, JobRunLog], Awaitable[dict[str, Any]]]
JOB_HANDLERS: dict[str, JobHandler] = {}


class JobOutcomeKind(StrEnum):
    SUCCESS = "success"
    CONTINUE = "continue"
    RETRY = "retry"
    MANUAL_REVIEW = "manual_review"
    SUPERSEDED = "superseded"
    FAILED = "failed"


@dataclass(frozen=True)
class JobOutcome:
    kind: JobOutcomeKind
    payload: dict[str, Any]
    error_code: str | None = None
    retry_after_seconds: int | None = None


_LOCAL_COMPLETION_JOB_TYPES = {
    "email_parse",
    "email_reparse",
    "imap_fetch",
    "mail_ingress_process",
    "ai_log_maintenance",
    "consistency_recovery",
    "master_data_import",
    "export_generate",
    "auto_followup",
}


def normalize_job_outcome(job: JobRunLog, result: dict[str, Any]) -> JobOutcome:
    """Translate domain statuses into an explicit queue outcome.

    Domain payloads are intentionally not interpreted by a permissive default.
    Every externally stateful job type has an allowlist; an unknown result is a
    failed execution rather than a false success.
    """
    if not isinstance(result, dict):
        return JobOutcome(
            JobOutcomeKind.FAILED,
            {"result_type": type(result).__name__},
            "JOB_OUTCOME_INVALID",
        )
    if str(result.get("status") or "").strip().lower() == "chunk_pending":
        return JobOutcome(JobOutcomeKind.CONTINUE, result)
    if job.job_type in _LOCAL_COMPLETION_JOB_TYPES:
        return JobOutcome(JobOutcomeKind.SUCCESS, result)

    status_value = str(result.get("status") or "").strip().lower()
    error_code = str(result.get("error_code") or "").strip().upper() or None

    if status_value == "superseded":
        return JobOutcome(
            JobOutcomeKind.SUPERSEDED,
            result,
            error_code or "TASK_SNAPSHOT_SUPERSEDED",
        )
    if status_value in {
        "manual_review",
        "send_uncertain",
        "pending_review",
        "reply_sent_rma_pending",
    }:
        return JobOutcome(
            JobOutcomeKind.MANUAL_REVIEW,
            result,
            error_code or status_value.upper(),
        )

    if job.job_type == "smtp_send":
        if status_value == "sent":
            return JobOutcome(JobOutcomeKind.SUCCESS, result)
        if status_value == "send_failed":
            return JobOutcome(JobOutcomeKind.FAILED, result, error_code or "SMTP_SEND_FAILED")
    elif job.job_type == "relay_ticket_export":
        if status_value in {"waiting_sap_result", "submit_unknown", "pending"}:
            return JobOutcome(
                JobOutcomeKind.RETRY,
                result,
                error_code,
                int(result.get("next_poll_seconds") or 300),
            )
        if status_value in {"submit_failed", "failed"}:
            return JobOutcome(JobOutcomeKind.FAILED, result, error_code or "SAP_SUBMIT_FAILED")
        if status_value in {"submitted", "succeeded", "completed"}:
            return JobOutcome(JobOutcomeKind.SUCCESS, result)
    elif job.job_type == "sap_rma_poll":
        if status_value in {"waiting_rma", "waiting_sap_result", "submit_unknown", "pending"}:
            return JobOutcome(
                JobOutcomeKind.RETRY,
                result,
                error_code,
                int(result.get("next_poll_seconds") or 300),
            )
        if status_value in {"submit_failed", "failed"}:
            return JobOutcome(JobOutcomeKind.FAILED, result, error_code or "SAP_POLL_FAILED")
        if status_value in {"rma_received", "idle", "completed", "succeeded"}:
            return JobOutcome(JobOutcomeKind.SUCCESS, result)
    elif job.job_type == "sap_submit_reconcile":
        if status_value == "submit_unknown":
            return JobOutcome(JobOutcomeKind.RETRY, result, error_code, 300)
        if status_value in {"waiting_sap_result", "pending", "completed", "succeeded"}:
            return JobOutcome(JobOutcomeKind.SUCCESS, result)
    elif job.job_type == "sap_sn_sync":
        if status_value == "succeeded":
            return JobOutcome(JobOutcomeKind.SUCCESS, result)
        if status_value in {"failed", "rejected"}:
            return JobOutcome(JobOutcomeKind.FAILED, result, error_code or "SAP_SN_SYNC_FAILED")
    elif job.job_type == "rma_authorization":
        if status_value in {
            "queued",
            "approved_pending_send",
            "sent",
            "succeeded",
            "closed",
            "not_required",
        }:
            return JobOutcome(JobOutcomeKind.SUCCESS, result)
    elif job.job_type == "rma_archive":
        if status_value in {"closed", "succeeded"}:
            return JobOutcome(JobOutcomeKind.SUCCESS, result)
        if status_value == "archive_failed":
            return JobOutcome(JobOutcomeKind.FAILED, result, error_code or "RMA_ARCHIVE_FAILED")
    elif job.job_type == "oss_delete":
        if status_value == "success":
            return JobOutcome(JobOutcomeKind.SUCCESS, result)
        if status_value == "failed":
            return JobOutcome(JobOutcomeKind.RETRY, result, error_code or "OSS_DELETE_PENDING", 300)

    return JobOutcome(
        JobOutcomeKind.FAILED,
        result,
        "JOB_OUTCOME_INVALID",
    )


def job_handler(*job_types: str) -> Callable[[JobHandler], JobHandler]:
    def register(handler: JobHandler) -> JobHandler:
        for job_type in job_types:
            if job_type in JOB_HANDLERS:
                raise RuntimeError(f"DUPLICATE_JOB_HANDLER:{job_type}")
            JOB_HANDLERS[job_type] = handler
        return handler

    return register


def _metadata(job: JobRunLog) -> tuple[dict[str, Any], int | None]:
    metadata = job.metadata_json or {}
    user_id = metadata.get("user_id") if isinstance(metadata.get("user_id"), int) else None
    return metadata, user_id


def _resource_id(job: JobRunLog) -> int:
    if job.resource_id is None:
        raise ValueError("JOB_RESOURCE_REQUIRED")
    return job.resource_id


@job_handler("email_parse", "email_reparse")
async def handle_email_parse(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.emails import reparse_email

    metadata, user_id = _metadata(job)
    return await reparse_email(
        session,
        email_id=_resource_id(job),
        user_id=user_id,
        reason="background parse",
        durable_attachment_stages=True,
        rule_parse_result_id=(
            int(metadata["rule_parse_result_id"])
            if isinstance(metadata.get("rule_parse_result_id"), int)
            else None
        ),
        mode="field_extract",
    )


@job_handler("imap_fetch")
async def handle_imap_fetch(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.imap_fetcher import run_imap_fetch_locked

    metadata, user_id = _metadata(job)
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


@job_handler("mail_ingress_process")
async def handle_mail_ingress(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.mail_processing import process_spooled_mail

    metadata, user_id = _metadata(job)
    return await process_spooled_mail(
        session,
        fetch_record_id=_resource_id(job),
        user_id=user_id,
        auto_parse=bool(metadata.get("auto_parse", True)),
    )


@job_handler("smtp_send")
async def handle_smtp_send(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.replies import execute_approved_reply_send

    _, user_id = _metadata(job)
    return await execute_approved_reply_send(session, reply_id=_resource_id(job), user_id=user_id)


@job_handler("relay_ticket_export")
async def handle_relay_export(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.relay_jobs import execute_ticket_relay_export

    return await execute_ticket_relay_export(session, export_id=_resource_id(job))


@job_handler("sap_rma_poll")
async def handle_sap_poll(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.sap_rma import poll_export_batch, poll_waiting_rma_results

    metadata, _ = _metadata(job)
    if job.resource_id is None:
        return await poll_waiting_rma_results(session)
    return await poll_export_batch(
        session,
        export_id=_resource_id(job),
        allow_late_result=bool(metadata.get("allow_late_result", False)),
        confirmed_by_user_id=(
            int(metadata["confirmed_by_user_id"])
            if isinstance(metadata.get("confirmed_by_user_id"), int)
            else None
        ),
    )


@job_handler("sap_submit_reconcile")
async def handle_sap_submit_reconcile(
    session: AsyncSession, job: JobRunLog
) -> dict[str, Any]:
    from app.services.sap_rma import reconcile_uncertain_submission

    metadata, user_id = _metadata(job)
    return await reconcile_uncertain_submission(
        session,
        export_id=_resource_id(job),
        reason=str(metadata.get("reason") or "queued reconciliation"),
        user_id=user_id,
    )


@job_handler("ai_log_maintenance")
async def handle_ai_log_maintenance(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.ai import maintain_ai_jsonl_logs

    metadata, _ = _metadata(job)
    result = await maintain_ai_jsonl_logs(
        session,
        start_after=str(metadata.get("chunk_cursor") or ""),
        max_files=20,
    )
    job.metadata_json = {
        **metadata,
        "chunk_cursor": result.get("next_cursor") or metadata.get("chunk_cursor") or "",
        "sanitized_files": int(metadata.get("sanitized_files") or 0)
        + int(result.get("sanitized_files") or 0),
        "deleted_files": int(metadata.get("deleted_files") or 0)
        + int(result.get("deleted_files") or 0),
    }
    return {
        "status": "chunk_pending" if result.get("has_more") else "completed",
        "sanitized_files": job.metadata_json["sanitized_files"],
        "deleted_files": job.metadata_json["deleted_files"],
        "processed_files": result.get("processed_files", 0),
    }


@job_handler("consistency_recovery")
async def handle_consistency_recovery(session: AsyncSession, _job: JobRunLog) -> dict[str, Any]:
    from app.services.jobs import recover_stale_jobs
    from app.services.notification_task_repair import repair_notification_and_task_data

    stale_jobs = await recover_stale_jobs(session)
    # Legacy queued jobs are intentionally audit-only. Mutating manual-task
    # repair must be an explicit operator action and must never run on a timer.
    result = await repair_notification_and_task_data(session, apply=False)
    return {"status": "completed", "stale_jobs": stale_jobs, **result}


@job_handler("sap_sn_sync")
async def handle_sap_sn_sync(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.sap_sn_sync import advance_sn_sync_job

    _, user_id = _metadata(job)
    return await advance_sn_sync_job(session, job=job, user_id=user_id)


@job_handler("rma_authorization")
async def handle_rma_authorization(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.replies import create_and_send_rma_authorization

    metadata, user_id = _metadata(job)
    return await create_and_send_rma_authorization(
        session,
        ticket_id=_resource_id(job),
        user_id=user_id,
        expected_version=int(metadata.get("ticket_version") or 0),
        expected_safety_hash=str(metadata.get("safety_check_hash") or ""),
        expected_sn_validation_hash=str(metadata.get("sn_validation_hash") or ""),
        expected_rma_template_version=str(metadata.get("rma_template_version") or ""),
        expected_rma_no=str(metadata.get("rma_no") or ""),
    )


@job_handler("rma_archive")
async def handle_rma_archive(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.replies import retry_rma_archive

    _, user_id = _metadata(job)
    return await retry_rma_archive(session, reply_id=_resource_id(job), user_id=user_id)


@job_handler("auto_followup")
async def handle_auto_followup(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.replies import create_reply_draft

    _, user_id = _metadata(job)
    return await create_reply_draft(session, ticket_id=_resource_id(job), user_id=user_id)


@job_handler("oss_delete")
async def handle_oss_delete(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services.deletions import process_oss_deletion_operation

    return await process_oss_deletion_operation(session, _resource_id(job))


@job_handler("master_data_import")
async def handle_master_data_import(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services import master_data
    from app.services.storage import download_oss_object_bytes

    metadata, user_id = _metadata(job)
    if job.input_oss_object_id is None or user_id is None:
        raise ValueError("JOB_RESOURCE_REQUIRED")
    content = await download_oss_object_bytes(session, oss_object_id=job.input_oss_object_id)
    kind = metadata.get("kind")
    cursor = max(0, int(metadata.get("chunk_cursor") or 0))
    chunk_size = max(10, min(500, int(metadata.get("chunk_size") or 100)))
    if kind == "sn_assets":
        items, file_hash = await asyncio.to_thread(master_data.parse_sn_assets_xlsx, content)
        result = await master_data.import_sn_assets(
            session,
            items=items[cursor : cursor + chunk_size],
            source_file_name=None,
            source_file_hash=file_hash,
            user_id=user_id,
            job=job,
        )
    elif kind == "board_cards":
        items, file_hash = await asyncio.to_thread(
            master_data.parse_board_cards_file, content, filename=metadata.get("filename")
        )
        result = await master_data.import_board_cards(
            session,
            items=items[cursor : cursor + chunk_size],
            source_file_name=metadata.get("filename"),
            source_file_hash=file_hash,
            user_id=user_id,
            job=job,
        )
    else:
        raise ValueError("MASTER_DATA_KIND_NOT_SUPPORTED")
    next_cursor = min(len(items), cursor + chunk_size)
    previous_created = int(metadata.get("chunk_created") or 0)
    previous_updated = int(metadata.get("chunk_updated") or 0)
    previous_skipped = int(metadata.get("chunk_skipped") or 0)
    cumulative = {
        **metadata,
        "chunk_cursor": next_cursor,
        "chunk_size": chunk_size,
        "chunk_total": len(items),
        "chunk_created": previous_created + int(result.get("created") or 0),
        "chunk_updated": previous_updated + int(result.get("updated") or 0),
        "chunk_skipped": previous_skipped + int(result.get("skipped") or 0),
        "source_file_hash": file_hash,
    }
    job.metadata_json = cumulative
    job.processed_count = next_cursor
    job.success_count = cumulative["chunk_created"] + cumulative["chunk_updated"]
    payload = {
        "status": "completed" if next_cursor >= len(items) else "chunk_pending",
        "job_run_id": job.id,
        "created": cumulative["chunk_created"],
        "updated": cumulative["chunk_updated"],
        "skipped": cumulative["chunk_skipped"],
        "processed": next_cursor,
        "total": len(items),
    }
    return payload


@job_handler("export_generate")
async def handle_export_generate(session: AsyncSession, job: JobRunLog) -> dict[str, Any]:
    from app.services import emails, master_data, tickets
    from app.services.storage import upload_bytes_to_oss

    metadata, user_id = _metadata(job)
    kind = metadata.get("kind")
    filters = metadata.get("filters") if isinstance(metadata.get("filters"), dict) else {}
    ids = metadata.get("ids") if isinstance(metadata.get("ids"), list) else []
    if kind == "sn_assets":
        content = await (master_data.export_sn_assets_selected(session, ids=ids) if ids else master_data.export_sn_assets(session, **filters))
    elif kind == "board_cards":
        content = await (master_data.export_board_cards_selected(session, ids=ids) if ids else master_data.export_board_cards(session, **filters))
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


def validate_job_handlers(job_types: set[str]) -> None:
    missing = job_types - JOB_HANDLERS.keys()
    extra = JOB_HANDLERS.keys() - job_types
    if missing or extra:
        raise RuntimeError(
            f"JOB_HANDLER_REGISTRY_MISMATCH:missing={','.join(sorted(missing))}:extra={','.join(sorted(extra))}"
        )


async def dispatch_job(session: AsyncSession, job: JobRunLog) -> JobOutcome:
    handler = JOB_HANDLERS.get(job.job_type)
    if handler is None:
        raise NotImplementedError(f"{job.job_type.upper()}_HANDLER_NOT_IMPLEMENTED")
    return normalize_job_outcome(job, await handler(session, job))
