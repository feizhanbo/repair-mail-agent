from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Email, MailFetchRecord, OssObject
from app.services.attachment_precheck import filter_decorative_attachments
from app.services.eml import attachment_blobs_from_eml_bytes, payload_from_eml_bytes
from app.services.dsn_parser import persist_dsn_event
from app.services.mail_ingress import process_preclassified_ingress
from app.services.mail_precheck import precheck_email_payload
from app.services.storage import delete_oss_object, download_oss_object_bytes


class MailProcessingError(RuntimeError):
    pass


async def _release_lifecycle_spool(
    session: AsyncSession,
    *,
    fetch_record: MailFetchRecord,
) -> None:
    object_id = fetch_record.raw_eml_oss_object_id
    if object_id is None:
        return
    durable_email_refs = int(
        await session.scalar(select(func.count()).select_from(Email).where(Email.raw_eml_oss_object_id == object_id))
        or 0
    )
    other_fetch_refs = int(
        await session.scalar(
            select(func.count()).select_from(MailFetchRecord).where(
                MailFetchRecord.raw_eml_oss_object_id == object_id,
                MailFetchRecord.id != fetch_record.id,
                MailFetchRecord.raw_retention_mode != "released",
            )
        )
        or 0
    )
    if durable_email_refs or other_fetch_refs:
        fetch_record.raw_retention_mode = "shared_reference"
        return
    oss_object = await session.get(OssObject, object_id, with_for_update=True)
    if oss_object is None:
        fetch_record.raw_eml_oss_object_id = None
        fetch_record.raw_retention_mode = "released"
        return
    await delete_oss_object(
        bucket=oss_object.bucket,
        endpoint=oss_object.endpoint,
        object_key=oss_object.object_key,
        object_version=oss_object.object_version,
    )
    fetch_record.raw_eml_oss_object_id = None
    fetch_record.raw_retention_mode = "released"
    await session.delete(oss_object)


async def process_spooled_mail(
    session: AsyncSession,
    *,
    fetch_record_id: int,
    user_id: int | None = None,
    auto_parse: bool = True,
) -> dict:
    fetch_record = await session.get(MailFetchRecord, fetch_record_id, with_for_update=True)
    if fetch_record is None:
        raise MailProcessingError("MAIL_FETCH_RECORD_NOT_FOUND")
    if fetch_record.fetch_status in {"completed", "classified_first", "classified_second", "classified_third", "classified_unknown"}:
        return {"status": "success", "fetch_record_id": fetch_record.id, "reused": True}
    if fetch_record.raw_eml_oss_object_id is None:
        raise MailProcessingError("MAIL_RAW_EML_NOT_SPOOLED")

    raw = await download_oss_object_bytes(session, oss_object_id=fetch_record.raw_eml_oss_object_id)
    dsn_event = await persist_dsn_event(
        session,
        raw_eml=raw,
        raw_sha256=fetch_record.raw_eml_sha256 or "",
    )
    if dsn_event is not None:
        fetch_record.fetch_status = "completed"
        fetch_record.processing_stage = "dsn_processed"
        fetch_record.raw_retention_mode = "permanent"
        return {
            "status": "success",
            "fetch_record_id": fetch_record.id,
            "delivery_event_id": dsn_event.id,
            "delivery_status": dsn_event.delivery_status,
        }
    payload = payload_from_eml_bytes(
        raw,
        mailbox_account=fetch_record.mailbox_account,
        folder_name=fetch_record.folder_name,
    )
    payload.imap_uid = fetch_record.imap_uid
    payload.fetch_job_run_id = fetch_record.fetch_job_run_id
    payload.raw_eml_oss_object_id = fetch_record.raw_eml_oss_object_id
    payload.raw_eml_sha256 = fetch_record.raw_eml_sha256
    blobs = attachment_blobs_from_eml_bytes(raw)
    blobs, _ = filter_decorative_attachments(payload, blobs)
    precheck = await precheck_email_payload(
        session,
        payload,
        enforce_target_mailbox=True,
        current_fetch_record_id=fetch_record.id,
    )
    if not precheck.accepted:
        # A second precheck can observe another worker's durable business
        # record. Treat deterministic skips as completion, never as a retry.
        fetch_record.fetch_status = precheck.status
        fetch_record.processing_stage = "precheck_completed"
        fetch_record.error_message = precheck.reason
        await _release_lifecycle_spool(session, fetch_record=fetch_record)
        return {"status": "success", "fetch_record_id": fetch_record.id, "fetch_status": precheck.status}

    fetch_record.fetch_status = "processing"
    fetch_record.processing_stage = "classifying"
    result = await process_preclassified_ingress(
        session,
        payload=payload,
        raw_eml=raw,
        raw_file_name=f"imap-{fetch_record.imap_uid}.eml",
        attachment_blobs=blobs,
        source="imap",
        precheck=precheck,
        user_id=user_id,
        auto_parse=auto_parse,
        fetch_record=fetch_record,
        uid_validity=int(fetch_record.uid_validity),
    )
    handling_level = str((result.get("classification") or {}).get("handling_level") or "")
    if handling_level == "lifecycle_only":
        await _release_lifecycle_spool(session, fetch_record=fetch_record)
    else:
        fetch_record.raw_retention_mode = "permanent"
    return {"status": "success", **result}
