from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import get_correlation_id
from app.models import MailFetchRecord, ManualReviewTask
from app.schemas.business import EmailIngestRequest
from app.services import emails as email_service
from app.services.common import utcnow
from app.services.email_archival import archive_email_bundle, archive_raw_email
from app.services.jobs import enqueue_job
from app.services.mail_precheck import MailPrecheckResult
from app.services.mail_preclassification import classify_mail, transient_attachment_evidence


def _needs_transient_evidence(payload: EmailIngestRequest, decision, blobs: list[dict[str, Any]]) -> bool:
    return bool(blobs) and (
        decision.needs_attachment_content
        or decision.reason_code in {"PRECLASSIFICATION_LOW_CONFIDENCE", "PRECLASSIFICATION_INTENT_CONFLICT"}
        or len((payload.text_body or "").strip()) < 80
    )


async def persist_missing_message_id_anomaly(
    session: AsyncSession,
    *,
    payload: EmailIngestRequest,
    raw_eml: bytes,
    raw_file_name: str,
    source: str,
    user_id: int | None,
) -> MailFetchRecord:
    """Preserve a non-IMAP raw source that cannot enter the RFC business chain."""
    source_hash = payload.raw_eml_sha256 or hashlib.sha256(raw_eml).hexdigest()
    anomaly_uid = f"{source}:{source_hash}"[:100]
    record = await session.scalar(
        select(MailFetchRecord).where(
            MailFetchRecord.mailbox_account == (payload.mailbox_account or "manual_ingest"),
            MailFetchRecord.folder_name == (payload.folder_name or source),
            MailFetchRecord.uid_validity == 0,
            MailFetchRecord.imap_uid == anomaly_uid,
        )
    )
    if record is not None and record.fetch_status == "terminal_manual":
        return record
    archived = await archive_raw_email(
        session,
        payload=payload,
        raw_eml=raw_eml,
        raw_file_name=raw_file_name,
        user_id=user_id,
    )
    if record is None:
        record = MailFetchRecord(
            mailbox_account=payload.mailbox_account or "manual_ingest",
            folder_name=payload.folder_name or source,
            uid_validity=0,
            imap_uid=anomaly_uid,
            message_id=None,
        )
        session.add(record)
    record.fetch_status = "terminal_manual"
    record.processing_stage = "header_validation_failed"
    record.error_message = "MISSING_MESSAGE_ID"
    record.raw_eml_oss_object_id = archived.raw_object_id
    record.raw_eml_sha256 = archived.source_content_sha256
    record.raw_retention_mode = "permanent"
    session.add(
        ManualReviewTask(
            task_type="missing_message_id",
            priority="high",
            status="pending",
            description="入站邮件缺少 RFC Message-ID，已保留原始 EML，禁止进入自动业务链。",
            trigger_reason="MISSING_MESSAGE_ID",
            recovery_stage="header_validation",
            recovery_action=f"核对来源 {source} 的原始邮件并决定是否人工处理。",
        )
    )
    await session.flush()
    return record


def _public_result(fetch_record: MailFetchRecord, decision, *, email: dict[str, Any] | None, job) -> dict[str, Any]:
    return {
        "fetch_record_id": fetch_record.id,
        "fetch_status": fetch_record.fetch_status,
        "email": email,
        "classification": {
            "intent_type": decision.intent_type,
            "handling_level": decision.handling_level,
            "confidence": decision.confidence,
            "reason_code": decision.reason_code,
            "evidence": decision.evidence,
        },
        "job_id": getattr(job, "id", None),
    }


async def process_preclassified_ingress(
    session: AsyncSession,
    *,
    payload: EmailIngestRequest,
    raw_eml: bytes,
    raw_file_name: str,
    attachment_blobs: list[dict[str, Any]],
    source: str,
    precheck: MailPrecheckResult,
    user_id: int | None,
    auto_parse: bool,
    fetch_record: MailFetchRecord | None = None,
    uid_validity: int = 0,
) -> dict[str, Any]:
    """The single classification-before-persistence policy for every inbound source."""
    if fetch_record is None:
        deterministic_uid = payload.imap_uid or f"{source}:{payload.raw_eml_sha256 or payload.message_id}"[:100]
        fetch_record = MailFetchRecord(
            mailbox_account=payload.mailbox_account,
            folder_name=payload.folder_name or source,
            uid_validity=uid_validity,
            imap_uid=deterministic_uid,
            message_id=payload.message_id,
            in_reply_to=payload.in_reply_to,
            references_header=payload.references_header,
            fetch_job_run_id=payload.fetch_job_run_id,
            fetch_status="processing",
            processing_stage="classifying",
            attempt_count=1,
            last_attempt_at=utcnow(),
        )
        session.add(fetch_record)
        await session.flush()
    else:
        fetch_record.in_reply_to = payload.in_reply_to
        fetch_record.references_header = payload.references_header
        fetch_record.processing_stage = "classifying"
    if callable(getattr(session, "commit", None)):
        await session.commit()

    thread_id = await email_service.find_existing_thread_anchor(
        session, in_reply_to=payload.in_reply_to, references_header=payload.references_header
    )
    thread_summary = await email_service.build_thread_classification_summary(session, thread_id=thread_id)
    decision = await classify_mail(
        payload, session=session, mail_fetch_record_id=fetch_record.id, thread_summary=thread_summary
    )
    if _needs_transient_evidence(payload, decision, attachment_blobs):
        evidence = transient_attachment_evidence(attachment_blobs)
        if evidence:
            decision = await classify_mail(
                payload, session=session, mail_fetch_record_id=fetch_record.id,
                thread_summary=thread_summary, attachment_evidence=evidence,
            )

    fetch_record.intent_type = decision.intent_type
    fetch_record.handling_level = decision.handling_level
    fetch_record.classification_version = decision.classification_version
    fetch_record.classification_confidence = decision.confidence
    fetch_record.classification_reason_code = decision.reason_code
    fetch_record.classification_evidence = {
        "candidates": decision.candidates,
        "evidence": decision.evidence,
        "needs_attachment_content": decision.needs_attachment_content,
    }
    fetch_record.thread_id = thread_id
    fetch_record.classified_at = utcnow()
    fetch_record.processing_stage = "routing"
    if callable(getattr(session, "commit", None)):
        await session.commit()

    try:
        return await _persist_classified_mail(
            session,
            payload=payload,
            raw_eml=raw_eml,
            raw_file_name=raw_file_name,
            attachment_blobs=attachment_blobs,
            source=source,
            precheck=precheck,
            user_id=user_id,
            auto_parse=auto_parse,
            fetch_record=fetch_record,
            decision=decision,
        )
    except Exception as exc:
        # Classification is already durable.  Preserve the exact restart boundary
        # when OSS or formal business persistence fails afterwards.
        if callable(getattr(session, "rollback", None)):
            await session.rollback()
        durable_record = await session.get(MailFetchRecord, fetch_record.id)
        if durable_record is not None:
            durable_record.fetch_status = "retry_wait"
            durable_record.processing_stage = "persistence_failed"
            durable_record.recovery_stage = (
                "route_first" if decision.handling_level == "auto_repair"
                else "route_minimal"
            )
            durable_record.error_message = getattr(exc, "code", exc.__class__.__name__)
            if callable(getattr(session, "commit", None)):
                await session.commit()
        raise


async def _persist_classified_mail(
    session: AsyncSession,
    *,
    payload: EmailIngestRequest,
    raw_eml: bytes,
    raw_file_name: str,
    attachment_blobs: list[dict[str, Any]],
    source: str,
    precheck: MailPrecheckResult,
    user_id: int | None,
    auto_parse: bool,
    fetch_record: MailFetchRecord,
    decision,
) -> dict[str, Any]:
    if decision.handling_level == "lifecycle_only":
        fetch_record.fetch_status = "classified_third"
        fetch_record.processing_stage = "third_completed"
        fetch_record.completed_at = utcnow()
        return _public_result(fetch_record, decision, email=None, job=None)

    if decision.handling_level in {"manual_rma_business", "unknown"}:
        await archive_raw_email(
            session, payload=payload, raw_eml=raw_eml, raw_file_name=raw_file_name, user_id=user_id
        )
        if callable(getattr(session, "commit", None)):
            await session.commit()
        ingest = await email_service.ingest_minimal_email(
            session, payload=payload, intent_type=decision.intent_type,
            handling_level=decision.handling_level, classification_confidence=decision.confidence,
            classification_reason_code=decision.reason_code,
            priority="high" if decision.handling_level == "unknown" else "normal",
        )
        fetch_record.email_id = ingest.get("email", {}).get("id")
        fetch_record.fetch_status = "classified_unknown" if decision.handling_level == "unknown" else "classified_second"
        fetch_record.processing_stage = "unknown_minimal" if decision.handling_level == "unknown" else "second_minimal"
        fetch_record.completed_at = utcnow()
        result = _public_result(fetch_record, decision, email=ingest.get("email"), job=None)
        result["manual_task_id"] = ingest.get("manual_task_id")
        return result

    await archive_email_bundle(
        session, payload=payload, raw_eml=raw_eml, raw_file_name=raw_file_name,
        attachment_blobs=attachment_blobs, source=source, user_id=user_id,
        correlation_id=get_correlation_id(),
    )
    if callable(getattr(session, "commit", None)):
        await session.commit()
    ingest = await email_service.ingest_email(
        session, payload=payload, user_id=user_id, auto_parse=False, rule_analysis=precheck.rule_analysis
    )
    email_id = int(ingest["email"]["id"])
    email = await session.get(email_service.Email, email_id)
    if email is not None and not ingest.get("duplicate"):
        email.intent_type = decision.intent_type
        email.handling_level = decision.handling_level
        email.classification_version = decision.classification_version
        email.classification_confidence = decision.confidence
        email.classification_reason_code = decision.reason_code
        email.persistence_tier = "business"
    job = None
    if auto_parse and not ingest.get("duplicate"):
        job = await enqueue_job(
            session, job_type="email_parse", resource_type="email", resource_id=email_id,
            idempotency_key=f"email_parse:{email_id}:initial", correlation_id=get_correlation_id(),
            metadata={
                "user_id": user_id, "reason": f"initial {source} parse",
                "rule_parse_result_id": ingest["rule_parse_result_id"], "mode": "field_extract",
            },
        )
    fetch_record.email_id = email_id
    fetch_record.fetch_status = "classified_first"
    fetch_record.processing_stage = "first_ingested"
    fetch_record.completed_at = utcnow()
    return _public_result(fetch_record, decision, email=ingest.get("email"), job=job)
