from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import make_url
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
from app.services.common import normalize_message_id, sha256_text, utcnow
from app.services.deletions import (
    _finalize_database_delete,
    _prepare_oss_operations,
    process_oss_deletion_operation,
)
from app.services.mail_safety import TEST_MAIL_SENDER, test_envelope_allowed


ACTIVE_JOB_STATUSES = {"queued", "running", "retry_wait"}
UNCERTAIN_EXTERNAL_STATUSES = {"planned", "running", "uncertain", "failed_retryable"}
TEST_DATABASE_NAME = "repair_system_test"
TEST_DATABASE_HOSTS = {"127.0.0.1", "localhost"}


class GoldReplayError(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def normalize_gold_message_id(value: str) -> str:
    normalized = normalize_message_id(str(value or "").strip())
    if not normalized or normalized.startswith("<sha256-"):
        raise GoldReplayError("GOLD_MESSAGE_ID_INVALID")
    return normalized


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ints(values: Iterable[int | None]) -> list[int]:
    return sorted({int(value) for value in values if value})


async def _scalar_ids(session: AsyncSession, statement) -> list[int]:
    return _ints((await session.execute(statement)).scalars().all())


async def assert_gold_replay_environment(session: AsyncSession) -> dict[str, Any]:
    url = make_url(settings.DATABASE_URL)
    database_name = str(await session.scalar(select(func.database())) or "")
    reasons: list[str] = []
    if settings.APP_ENV.strip().lower() not in {"dev", "test"}:
        reasons.append("APP_ENV_MUST_BE_DEV_OR_TEST")
    if database_name != TEST_DATABASE_NAME or url.database != TEST_DATABASE_NAME:
        reasons.append("DATABASE_MUST_BE_REPAIR_SYSTEM_TEST")
    if (url.host or "").lower() not in TEST_DATABASE_HOSTS or int(url.port or 3306) != 13307:
        reasons.append("DATABASE_MUST_USE_LOOPBACK_13307")
    if not settings.E2E_GOLD_RUN_ENABLED:
        reasons.append("E2E_GOLD_RUN_ENABLED_REQUIRED")
    if not settings.RUN_REAL_MAIL_INTEGRATION_TESTS:
        reasons.append("RUN_REAL_MAIL_INTEGRATION_TESTS_REQUIRED")
    if settings.RELAY_ADAPTER != "test_http":
        reasons.append("TEST_HTTP_RELAY_REQUIRED")
    if reasons:
        raise GoldReplayError("GOLD_REPLAY_ENVIRONMENT_BLOCKED", details={"reasons": reasons})
    return {
        "app_env": settings.APP_ENV,
        "database": database_name,
        "database_host": url.host,
        "database_port": url.port,
        "relay_adapter": settings.RELAY_ADAPTER,
    }


async def plan_gold_test_reset(
    session: AsyncSession,
    *,
    message_ids: list[str],
    require_existing: bool = False,
) -> dict[str, Any]:
    normalized = sorted({normalize_gold_message_id(value) for value in message_ids})
    if not normalized:
        raise GoldReplayError("GOLD_MESSAGE_IDS_REQUIRED")
    if len(normalized) > 50:
        raise GoldReplayError("GOLD_MESSAGE_ID_LIMIT_EXCEEDED")

    seed_emails = list((await session.execute(select(Email).where(Email.message_id.in_(normalized)))).scalars().all())
    standalone_fetch_records = list(
        (
            await session.execute(
                select(MailFetchRecord).where(MailFetchRecord.message_id.in_(normalized))
            )
        ).scalars().all()
    )
    found = {row.message_id for row in seed_emails}
    missing = sorted(set(normalized) - found)
    if missing and require_existing:
        raise GoldReplayError("GOLD_SEED_EMAIL_NOT_FOUND", details={"message_id_hashes": [sha256_text(v) for v in missing]})
    if not seed_emails and not standalone_fetch_records:
        empty = {
            "schema_version": 1,
            "message_id_hashes": [sha256_text(value) for value in normalized],
            "resource_ids": {name: [] for name in (
                "emails", "threads", "tickets", "attachments", "parse_results", "ticket_items",
                "replies", "rmas", "relay_exports", "sap_exports", "manual_tasks", "notifications",
                "ai_logs", "external_operations", "operation_logs", "system_logs", "jobs",
                "mail_fetch_records", "oss_objects",
            )},
            "affected_counts": {},
            "blockers": [],
            "already_clean": True,
        }
        empty["plan_hash"] = _stable_hash(empty)
        return empty
    if not seed_emails:
        fetch_record_ids = _ints(row.id for row in standalone_fetch_records)
        job_id_list = _ints(row.fetch_job_run_id for row in standalone_fetch_records)
        jobs = list(
            (
                await session.execute(
                    select(JobRunLog).where(JobRunLog.id.in_(job_id_list or [-1]))
                )
            ).scalars().all()
        )
        active_jobs = [row.id for row in jobs if row.status in ACTIVE_JOB_STATUSES]
        outside_job_emails = await _scalar_ids(
            session,
            select(Email.id).where(Email.fetch_job_run_id.in_(job_id_list or [-1])),
        )
        outside_job_fetch_records = await _scalar_ids(
            session,
            select(MailFetchRecord.id).where(
                MailFetchRecord.fetch_job_run_id.in_(job_id_list or [-1]),
                ~MailFetchRecord.id.in_(fetch_record_ids or [-1]),
            ),
        )
        system_ids = await _scalar_ids(
            session,
            select(SystemEventLog.id).where(SystemEventLog.job_run_id.in_(job_id_list or [-1])),
        )
        object_ids = _ints(
            [
                *(row.input_oss_object_id for row in jobs),
                *(row.output_oss_object_id for row in jobs),
            ]
        )
        blockers: list[str] = []
        if active_jobs:
            blockers.append("GOLD_REPLAY_ACTIVE_JOBS")
        if outside_job_emails or outside_job_fetch_records:
            blockers.append("GOLD_REPLAY_JOB_HAS_OUTSIDE_MAIL")
        resource_ids = {
            name: []
            for name in (
                "emails", "threads", "tickets", "attachments", "parse_results",
                "ticket_items", "replies", "rmas", "relay_exports", "sap_exports",
                "manual_tasks", "notifications", "ai_logs", "external_operations",
                "operation_logs", "system_logs", "jobs", "mail_fetch_records",
                "oss_objects",
            )
        }
        resource_ids.update(
            {
                "system_logs": system_ids,
                "jobs": job_id_list,
                "mail_fetch_records": fetch_record_ids,
                "oss_objects": object_ids,
            }
        )
        plan = {
            "schema_version": 1,
            "message_id_hashes": [sha256_text(value) for value in normalized],
            "resource_ids": resource_ids,
            "affected_counts": {
                name: len(values) for name, values in resource_ids.items() if values
            },
            "blockers": sorted(set(blockers)),
            "active_job_ids": active_jobs,
            "uncertain_external_operation_ids": [],
            "already_clean": False,
        }
        plan["plan_hash"] = _stable_hash(plan)
        return plan

    seed_ids = _ints(row.id for row in seed_emails)
    thread_ids = _ints(row.thread_id for row in seed_emails)
    ticket_ids = set(await _scalar_ids(session, select(RepairTicket.id).where(RepairTicket.source_email_id.in_(seed_ids))))
    ticket_ids.update(await _scalar_ids(session, select(EmailTicketLink.ticket_id).where(EmailTicketLink.email_id.in_(seed_ids))))
    ticket_ids.update(await _scalar_ids(session, select(ParseResult.ticket_id).where(ParseResult.email_id.in_(seed_ids), ParseResult.ticket_id.is_not(None))))
    if thread_ids:
        ticket_ids.update(await _scalar_ids(session, select(RepairTicket.id).where(RepairTicket.thread_id.in_(thread_ids))))
        ticket_ids.update(await _scalar_ids(session, select(EmailThread.ticket_id).where(EmailThread.id.in_(thread_ids), EmailThread.ticket_id.is_not(None))))
        ticket_ids.update(await _scalar_ids(session, select(EmailThread.predecessor_ticket_id).where(EmailThread.id.in_(thread_ids), EmailThread.predecessor_ticket_id.is_not(None))))

    email_ids = set(seed_ids)
    if thread_ids:
        email_ids.update(await _scalar_ids(session, select(Email.id).where(Email.thread_id.in_(thread_ids))))
    if ticket_ids:
        email_ids.update(await _scalar_ids(session, select(RepairTicket.source_email_id).where(RepairTicket.id.in_(ticket_ids), RepairTicket.source_email_id.is_not(None))))
        email_ids.update(await _scalar_ids(session, select(EmailTicketLink.email_id).where(EmailTicketLink.ticket_id.in_(ticket_ids))))
        email_ids.update(await _scalar_ids(session, select(ReplyRecord.related_email_id).where(ReplyRecord.ticket_id.in_(ticket_ids), ReplyRecord.related_email_id.is_not(None))))
        email_ids.update(await _scalar_ids(session, select(ReplyRecord.outgoing_email_id).where(ReplyRecord.ticket_id.in_(ticket_ids), ReplyRecord.outgoing_email_id.is_not(None))))
    # A newly discovered email can introduce the ticket for a sidecar/manual path.
    ticket_ids.update(await _scalar_ids(session, select(EmailTicketLink.ticket_id).where(EmailTicketLink.email_id.in_(email_ids))))
    ticket_ids.update(await _scalar_ids(session, select(ParseResult.ticket_id).where(ParseResult.email_id.in_(email_ids), ParseResult.ticket_id.is_not(None))))

    emails = list((await session.execute(select(Email).where(Email.id.in_(email_ids)))).scalars().all())
    thread_ids = _ints([*thread_ids, *(row.thread_id for row in emails)])
    if thread_ids:
        email_ids.update(await _scalar_ids(session, select(Email.id).where(Email.thread_id.in_(thread_ids))))
        emails = list((await session.execute(select(Email).where(Email.id.in_(email_ids)))).scalars().all())

    ticket_id_list = sorted(ticket_ids)
    attachment_ids = await _scalar_ids(session, select(EmailAttachment.id).where(EmailAttachment.email_id.in_(email_ids)))
    parse_ids = await _scalar_ids(session, select(ParseResult.id).where(ParseResult.email_id.in_(email_ids)))
    item_ids = await _scalar_ids(session, select(RepairTicketItem.id).where(RepairTicketItem.ticket_id.in_(ticket_id_list or [-1])))
    reply_ids = await _scalar_ids(session, select(ReplyRecord.id).where(ReplyRecord.ticket_id.in_(ticket_id_list or [-1])))
    rma_ids = await _scalar_ids(session, select(TicketRma.id).where(TicketRma.ticket_id.in_(ticket_id_list or [-1])))
    relay_ids = await _scalar_ids(session, select(TicketRelayExport.id).where(TicketRelayExport.ticket_id.in_(ticket_id_list or [-1])))
    sap_ids = await _scalar_ids(session, select(ExportSap.id).where(ExportSap.ticket_id.in_(ticket_id_list or [-1])))
    task_ids = await _scalar_ids(session, select(ManualReviewTask.id).where(or_(ManualReviewTask.ticket_id.in_(ticket_id_list or [-1]), ManualReviewTask.email_id.in_(email_ids), ManualReviewTask.thread_id.in_(thread_ids or [-1]))))
    notification_ids = await _scalar_ids(session, select(NotificationEvent.id).where(or_(NotificationEvent.ticket_id.in_(ticket_id_list or [-1]), (NotificationEvent.target_type.in_(["ticket", "repair_ticket"])) & NotificationEvent.target_id.in_(ticket_id_list or [-1]), (NotificationEvent.target_type == "manual_review_task") & NotificationEvent.target_id.in_(task_ids or [-1]))))
    ai_ids = await _scalar_ids(session, select(AiCallLog.id).where(or_(AiCallLog.email_id.in_(email_ids), AiCallLog.ticket_id.in_(ticket_id_list or [-1]), AiCallLog.attachment_id.in_(attachment_ids or [-1]))))
    external_ids = await _scalar_ids(session, select(ExternalOperationRecord.id).where(or_(ExternalOperationRecord.email_id.in_(email_ids), ExternalOperationRecord.ticket_id.in_(ticket_id_list or [-1]), ExternalOperationRecord.reply_record_id.in_(reply_ids or [-1]), ExternalOperationRecord.export_sap_id.in_(sap_ids or [-1]))))
    operation_ids = await _scalar_ids(session, select(OperationLog.id).where(or_(OperationLog.email_id.in_(email_ids), OperationLog.ticket_id.in_(ticket_id_list or [-1]))))
    system_ids = await _scalar_ids(session, select(SystemEventLog.id).where(or_(SystemEventLog.email_id.in_(email_ids), SystemEventLog.ticket_id.in_(ticket_id_list or [-1]))))

    fetch_job_ids = _ints(row.fetch_job_run_id for row in emails)
    job_ids = set(fetch_job_ids)
    job_ids.update(await _scalar_ids(session, select(JobRunLog.id).where(or_(
        (JobRunLog.resource_type == "email") & JobRunLog.resource_id.in_(email_ids),
        (JobRunLog.resource_type.in_(["ticket", "repair_ticket"])) & JobRunLog.resource_id.in_(ticket_id_list or [-1]),
        (JobRunLog.resource_type == "reply_record") & JobRunLog.resource_id.in_(reply_ids or [-1]),
        (JobRunLog.resource_type == "ticket_relay_export") & JobRunLog.resource_id.in_(relay_ids or [-1]),
    ))))
    job_ids.update(await _scalar_ids(session, select(AiCallLog.job_run_id).where(AiCallLog.id.in_(ai_ids or [-1]), AiCallLog.job_run_id.is_not(None))))
    job_id_list = sorted(job_ids)
    system_ids = sorted(set(system_ids) | set(await _scalar_ids(session, select(SystemEventLog.id).where(SystemEventLog.job_run_id.in_(job_id_list or [-1])))))

    fetch_record_ids = await _scalar_ids(session, select(MailFetchRecord.id).where(or_(MailFetchRecord.email_id.in_(email_ids), MailFetchRecord.message_id.in_(normalized))))
    attachments = list((await session.execute(select(EmailAttachment).where(EmailAttachment.id.in_(attachment_ids or [-1])))).scalars().all())
    replies = list((await session.execute(select(ReplyRecord).where(ReplyRecord.id.in_(reply_ids or [-1])))).scalars().all())
    rmas = list((await session.execute(select(TicketRma).where(TicketRma.id.in_(rma_ids or [-1])))).scalars().all())
    jobs = list((await session.execute(select(JobRunLog).where(JobRunLog.id.in_(job_id_list or [-1])))).scalars().all())
    object_ids = _ints([
        *(row.raw_eml_oss_object_id for row in emails),
        *(row.oss_object_id for row in attachments),
        *(row.rma_pdf_oss_object_id for row in replies),
        *(row.pdf_oss_object_id for row in rmas),
        *(row.input_oss_object_id for row in jobs),
        *(row.output_oss_object_id for row in jobs),
    ])

    blockers: list[str] = []
    extra_emails = [row for row in emails if row.id not in seed_ids]
    for row in extra_emails:
        if row.mail_direction == "outbound":
            related = [reply for reply in replies if reply.outgoing_email_id == row.id]
            if not related or not all(test_envelope_allowed(reply.to_addresses, reply.cc_addresses) for reply in related):
                blockers.append(f"NON_TEST_OUTBOUND_EMAIL:{row.id}")
        elif TEST_MAIL_SENDER.lower() not in str(row.to_addresses or "").lower():
            blockers.append(f"THREAD_EMAIL_NOT_DELIVERED_TO_TEST_MAILBOX:{row.id}")

    active_jobs = [row.id for row in jobs if row.status in ACTIVE_JOB_STATUSES]
    if active_jobs:
        blockers.append("GOLD_REPLAY_ACTIVE_JOBS")
    external_rows = list((await session.execute(select(ExternalOperationRecord).where(ExternalOperationRecord.id.in_(external_ids or [-1])))).scalars().all())
    uncertain_external = [row.id for row in external_rows if row.status in UNCERTAIN_EXTERNAL_STATUSES]
    if uncertain_external:
        blockers.append("GOLD_REPLAY_UNCERTAIN_EXTERNAL_OPERATION")
    outside_links = await _scalar_ids(session, select(EmailTicketLink.id).where(EmailTicketLink.ticket_id.in_(ticket_id_list or [-1]), ~EmailTicketLink.email_id.in_(email_ids)))
    if outside_links:
        blockers.append("GOLD_REPLAY_TICKET_HAS_OUTSIDE_EMAIL")
    successor_threads = await _scalar_ids(session, select(EmailThread.id).where(EmailThread.predecessor_thread_id.in_(thread_ids or [-1]), ~EmailThread.id.in_(thread_ids or [-1])))
    if successor_threads:
        blockers.append("GOLD_REPLAY_THREAD_HAS_OUTSIDE_SUCCESSOR")
    outside_job_emails = await _scalar_ids(
        session,
        select(Email.id).where(
            Email.fetch_job_run_id.in_(job_id_list or [-1]),
            ~Email.id.in_(email_ids),
        ),
    )
    outside_job_fetch_records = await _scalar_ids(
        session,
        select(MailFetchRecord.id).where(
            MailFetchRecord.fetch_job_run_id.in_(job_id_list or [-1]),
            ~MailFetchRecord.id.in_(fetch_record_ids or [-1]),
        ),
    )
    if outside_job_emails or outside_job_fetch_records:
        blockers.append("GOLD_REPLAY_JOB_HAS_OUTSIDE_MAIL")

    resource_ids = {
        "emails": sorted(email_ids), "threads": thread_ids, "tickets": ticket_id_list,
        "attachments": attachment_ids, "parse_results": parse_ids, "ticket_items": item_ids,
        "replies": reply_ids, "rmas": rma_ids, "relay_exports": relay_ids,
        "sap_exports": sap_ids, "manual_tasks": task_ids, "notifications": notification_ids,
        "ai_logs": ai_ids, "external_operations": external_ids, "operation_logs": operation_ids,
        "system_logs": system_ids, "jobs": job_id_list, "mail_fetch_records": fetch_record_ids,
        "oss_objects": object_ids,
    }
    affected_counts = {name: len(values) for name, values in resource_ids.items() if values}
    plan = {
        "schema_version": 1,
        "message_id_hashes": [sha256_text(value) for value in normalized],
        "resource_ids": resource_ids,
        "affected_counts": affected_counts,
        "blockers": sorted(set(blockers)),
        "active_job_ids": active_jobs,
        "uncertain_external_operation_ids": uncertain_external,
        "already_clean": False,
    }
    plan["plan_hash"] = _stable_hash(plan)
    return plan


async def apply_gold_test_reset(
    session: AsyncSession,
    *,
    message_ids: list[str],
    expected_plan_hash: str,
    suite_id: str,
    run_id: str,
    user_id: int,
    reason: str,
) -> dict[str, Any]:
    await assert_gold_replay_environment(session)
    plan = await plan_gold_test_reset(session, message_ids=message_ids)
    if plan["plan_hash"] != expected_plan_hash:
        raise GoldReplayError("GOLD_REPLAY_PLAN_STALE", details={"actual_plan_hash": plan["plan_hash"]})
    if plan["blockers"]:
        raise GoldReplayError("GOLD_REPLAY_DELETE_BLOCKED", details={"blockers": plan["blockers"]})
    if plan["already_clean"]:
        return {"status": "already_clean", "plan_hash": plan["plan_hash"], "affected_counts": {}}

    ids = plan["resource_ids"]
    audit = await log_operation(
        session,
        operation_type="gold_test_replay_reset",
        target_type="gold_test_suite",
        target_id=None,
        user_id=user_id,
        correlation_id=run_id,
        description=reason,
        before_data={
            "suite_id": suite_id,
            "run_id": run_id,
            "plan_hash": plan["plan_hash"],
            "message_id_hashes": plan["message_id_hashes"],
            "affected_counts": plan["affected_counts"],
        },
        after_data={"database_status": "planned", "oss_status": "pending"},
    )
    await session.flush()

    object_ids = ids["oss_objects"]
    removed_counts: dict[int, int] = {}
    for model, column, selected in (
        (Email, Email.raw_eml_oss_object_id, ids["emails"]),
        (EmailAttachment, EmailAttachment.oss_object_id, ids["attachments"]),
        (ReplyRecord, ReplyRecord.rma_pdf_oss_object_id, ids["replies"]),
        (TicketRma, TicketRma.pdf_oss_object_id, ids["rmas"]),
        (JobRunLog, JobRunLog.input_oss_object_id, ids["jobs"]),
        (JobRunLog, JobRunLog.output_oss_object_id, ids["jobs"]),
    ):
        if selected:
            values = (await session.execute(select(column).select_from(model).where(model.id.in_(selected), column.is_not(None)))).scalars().all()
            for value in values:
                removed_counts[int(value)] = removed_counts.get(int(value), 0) + 1
    oss_operation_ids, shared_ids = await _prepare_oss_operations(
        session, audit=audit, object_ids=object_ids, removed_reference_counts=removed_counts
    )
    if shared_ids:
        raise GoldReplayError("GOLD_REPLAY_SHARED_OSS_OBJECT", details={"oss_object_ids": shared_ids})

    await session.execute(delete(NotificationUserState).where(NotificationUserState.notification_id.in_(ids["notifications"] or [-1])))
    await session.execute(delete(NotificationEvent).where(NotificationEvent.id.in_(ids["notifications"] or [-1])))
    # These audit/event rows carry direct email/ticket/job foreign keys and
    # must be removed before any of their referenced business parents.
    await session.execute(delete(SystemEventLog).where(SystemEventLog.id.in_(ids["system_logs"] or [-1])))
    await session.execute(delete(OperationLog).where(OperationLog.id.in_(ids["operation_logs"] or [-1])))
    await session.execute(update(ReplyRecord).where(ReplyRecord.id.in_(ids["replies"] or [-1])).values(ai_call_log_id=None))
    await session.execute(update(TicketRma).where(TicketRma.id.in_(ids["rmas"] or [-1])).values(reply_record_id=None))
    await session.execute(delete(TicketRmaItem).where(TicketRmaItem.ticket_rma_id.in_(ids["rmas"] or [-1])))
    await session.execute(delete(TicketRma).where(TicketRma.id.in_(ids["rmas"] or [-1])))
    await session.execute(delete(ExternalOperationRecord).where(ExternalOperationRecord.id.in_(ids["external_operations"] or [-1])))
    await session.execute(delete(ReplyRecord).where(ReplyRecord.id.in_(ids["replies"] or [-1])))
    await session.execute(delete(ExportSap).where(ExportSap.id.in_(ids["sap_exports"] or [-1])))
    await session.execute(delete(TicketRelayExport).where(TicketRelayExport.id.in_(ids["relay_exports"] or [-1])))
    await session.execute(delete(FieldAuditLog).where(or_(FieldAuditLog.ticket_id.in_(ids["tickets"] or [-1]), FieldAuditLog.parse_result_id.in_(ids["parse_results"] or [-1]))))
    await session.execute(delete(TicketStatusLog).where(TicketStatusLog.ticket_id.in_(ids["tickets"] or [-1])))
    await session.execute(delete(SnValidationResult).where(SnValidationResult.ticket_id.in_(ids["tickets"] or [-1])))
    await session.execute(delete(ManualReviewTask).where(ManualReviewTask.id.in_(ids["manual_tasks"] or [-1])))
    await session.execute(delete(EmailTicketLink).where(or_(EmailTicketLink.email_id.in_(ids["emails"] or [-1]), EmailTicketLink.ticket_id.in_(ids["tickets"] or [-1]))))
    await session.execute(delete(ParseResult).where(ParseResult.id.in_(ids["parse_results"] or [-1])))
    await session.execute(delete(AiCallLog).where(AiCallLog.id.in_(ids["ai_logs"] or [-1])))
    await session.execute(delete(RepairTicketItem).where(RepairTicketItem.id.in_(ids["ticket_items"] or [-1])))
    await session.execute(
        update(EmailThread)
        .where(EmailThread.id.in_(ids["threads"] or [-1]))
        .values(
            latest_email_id=None,
            ticket_id=None,
            predecessor_ticket_id=None,
            predecessor_thread_id=None,
        )
    )
    await session.execute(delete(RepairTicket).where(RepairTicket.id.in_(ids["tickets"] or [-1])))
    await session.execute(delete(EmailAttachment).where(EmailAttachment.id.in_(ids["attachments"] or [-1])))
    await session.execute(update(Email).where(Email.duplicate_of_email_id.in_(ids["emails"] or [-1])).values(duplicate_of_email_id=None))
    await session.execute(delete(MailFetchRecord).where(MailFetchRecord.id.in_(ids["mail_fetch_records"] or [-1])))
    await session.execute(delete(Email).where(Email.id.in_(ids["emails"] or [-1])))
    await session.execute(delete(EmailThread).where(EmailThread.id.in_(ids["threads"] or [-1])))
    await session.execute(delete(JobRunLog).where(JobRunLog.id.in_(ids["jobs"] or [-1])))

    job = await _finalize_database_delete(
        session,
        audit=audit,
        operation_ids=oss_operation_ids,
        shared_ids=[],
        affected_counts=plan["affected_counts"],
        user_id=user_id,
    )
    await session.commit()
    oss_result = await process_oss_deletion_operation(session, audit.id) if oss_operation_ids else {"failed_count": 0}
    await session.commit()
    return {
        "status": "cleaned" if not oss_result.get("failed_count") else "cleanup_pending",
        "audit_log_id": audit.id,
        "job_id": job.id if job else None,
        "plan_hash": plan["plan_hash"],
        "affected_counts": plan["affected_counts"],
        "oss_failed_count": int(oss_result.get("failed_count") or 0),
    }


async def verify_gold_test_reset(session: AsyncSession, *, message_ids: list[str]) -> dict[str, Any]:
    normalized = sorted({normalize_gold_message_id(value) for value in message_ids})
    email_count = int(await session.scalar(select(func.count()).select_from(Email).where(Email.message_id.in_(normalized))) or 0)
    fetch_count = int(await session.scalar(select(func.count()).select_from(MailFetchRecord).where(MailFetchRecord.message_id.in_(normalized))) or 0)
    result = {
        "verified": email_count == 0 and fetch_count == 0,
        "email_count": email_count,
        "mail_fetch_record_count": fetch_count,
        "verified_at": utcnow().isoformat(),
    }
    if not result["verified"]:
        raise GoldReplayError("GOLD_REPLAY_VERIFY_FAILED", details=result)
    return result
