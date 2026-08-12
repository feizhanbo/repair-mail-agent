from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, or_, select

from app.core.database import AsyncSessionLocal
from app.config import settings
from app.models import (
    AiCallLog,
    Email,
    EmailAttachment,
    EmailThread,
    EmailTicketLink,
    ExportSap,
    ExternalOperationRecord,
    JobRunLog,
    OperationLog,
    OssObject,
    RepairTicket,
    RepairTicketItem,
    ReplyRecord,
    SystemEventLog,
    TicketRelayExport,
    TicketRma,
    User,
)
from app.services import deletions
from app.services.storage import OssDeleteResult, StorageDeleteError


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DELETE_INTEGRATION_TESTS") != "1",
    reason="destructive integration test is opt-in",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _cleanup_stale_test_residue(session) -> None:
    """Remove only fixtures produced by an interrupted DELETE-E2E test run."""
    stale_audits = list(
        (
            await session.execute(
                select(OperationLog.id).where(OperationLog.description.like("DELETE-E2E-%"))
            )
        ).scalars()
    )
    if stale_audits:
        stale_jobs = list(
            (
                await session.execute(
                    select(JobRunLog.id).where(
                        JobRunLog.resource_type == "operation_log",
                        JobRunLog.resource_id.in_(stale_audits),
                    )
                )
            ).scalars()
        )
        await session.execute(delete(SystemEventLog).where(SystemEventLog.job_run_id.in_(stale_jobs or [-1])))
        await session.execute(
            delete(ExternalOperationRecord).where(
                or_(
                    *[
                        ExternalOperationRecord.operation_key.like(f"delete:{audit_id}:%")
                        for audit_id in stale_audits
                    ]
                )
            )
        )
        await session.execute(delete(JobRunLog).where(JobRunLog.id.in_(stale_jobs or [-1])))
        await session.execute(delete(OperationLog).where(OperationLog.id.in_(stale_audits)))
    stale_emails = list(
        (
            await session.execute(
                select(Email.id).where(
                    Email.mailbox_account == "delete-e2e@invalid.local",
                    Email.subject.like("DELETE-E2E-%"),
                )
            )
        ).scalars()
    )
    await session.execute(delete(EmailTicketLink).where(EmailTicketLink.email_id.in_(stale_emails or [-1])))
    await session.execute(delete(EmailAttachment).where(EmailAttachment.email_id.in_(stale_emails or [-1])))
    await session.execute(delete(Email).where(Email.id.in_(stale_emails or [-1])))
    await session.execute(delete(OssObject).where(OssObject.source_type == "delete_e2e"))
    await session.commit()


@pytest.mark.anyio
async def test_temporary_delete_aggregates_and_preserves_ticket_source_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = f"DELETE-E2E-{uuid4().hex[:12]}"
    created_email_ids: list[int] = []
    created_object_ids: list[int] = []
    created_job_ids: list[int] = []
    created_ticket_ids: list[int] = []
    created_ai_ids: list[int] = []
    created_reply_ids: list[int] = []
    created_thread_ids: list[int] = []
    audit_ids: list[int] = []
    physically_deleted: list[tuple[str, str]] = []
    fail_once: set[str] = set()

    async def fake_delete_oss_object(**kwargs) -> OssDeleteResult:
        if kwargs["object_key"] in fail_once:
            fail_once.remove(kwargs["object_key"])
            raise StorageDeleteError("OSS_DELETE_FAILED")
        physically_deleted.append((kwargs["bucket"], kwargs["object_key"]))
        return OssDeleteResult(
            bucket=kwargs["bucket"],
            object_key=kwargs["object_key"],
            deleted=True,
        )

    monkeypatch.setattr(deletions, "delete_oss_object", fake_delete_oss_object)

    async with AsyncSessionLocal() as session:
        database_name = str(await session.scalar(select(func.database())) or "")
        assert database_name == "repair_system_test"
        await _cleanup_stale_test_residue(session)
        user_id = await session.scalar(select(User.id).order_by(User.id).limit(1))
        assert user_id is not None, "integration database requires one existing user for audit ownership"

        try:
            # 1) Independent attachment deletion.
            attachment_email = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-attachment@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
            )
            attachment_object = OssObject(
                bucket="delete-e2e-bucket",
                object_key=f"{batch}/attachment.bin",
                source_type="delete_e2e",
                upload_status="success",
            )
            session.add_all([attachment_email, attachment_object])
            await session.flush()
            created_email_ids.append(attachment_email.id)
            created_object_ids.append(attachment_object.id)
            attachment = EmailAttachment(
                email_id=attachment_email.id,
                oss_object_id=attachment_object.id,
                file_name="attachment.bin",
                parse_status="pending",
            )
            session.add(attachment)
            await session.commit()

            preview = await deletions.preview_attachment(session, attachment.id, int(user_id))
            assert preview["deletable"] is True
            result = await deletions.delete_attachment(
                session,
                attachment_id=attachment.id,
                user_id=int(user_id),
                reason=batch,
                confirmation_token=preview["confirmation_token"],
            )
            audit_ids.append(result["deletion_operation_id"])
            assert await session.get(EmailAttachment, attachment.id) is None
            assert await session.get(OssObject, attachment_object.id) is None
            assert await session.get(Email, attachment_email.id) is not None
            with pytest.raises(deletions.DeletionError) as missing_attachment:
                await deletions.preview_attachment(session, attachment.id, int(user_id))
            assert missing_attachment.value.status_code == 404

            shared_email_a = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-shared-a@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
            )
            shared_email_b = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-shared-b@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
            )
            shared_object = OssObject(
                bucket="delete-e2e-bucket",
                object_key=f"{batch}/shared.bin",
                source_type="delete_e2e",
                upload_status="success",
            )
            session.add_all([shared_email_a, shared_email_b, shared_object])
            await session.flush()
            created_email_ids.extend([shared_email_a.id, shared_email_b.id])
            created_object_ids.append(shared_object.id)
            shared_attachment_a = EmailAttachment(
                email_id=shared_email_a.id,
                oss_object_id=shared_object.id,
                file_name="shared-a.bin",
                parse_status="pending",
            )
            shared_attachment_b = EmailAttachment(
                email_id=shared_email_b.id,
                oss_object_id=shared_object.id,
                file_name="shared-b.bin",
                parse_status="pending",
            )
            session.add_all([shared_attachment_a, shared_attachment_b])
            await session.commit()
            shared_deleted_before = len(physically_deleted)
            preview = await deletions.preview_attachment(session, shared_attachment_a.id, int(user_id))
            result = await deletions.delete_attachment(
                session,
                attachment_id=shared_attachment_a.id,
                user_id=int(user_id),
                reason=batch,
                confirmation_token=preview["confirmation_token"],
            )
            audit_ids.append(result["deletion_operation_id"])
            assert await session.get(EmailAttachment, shared_attachment_a.id) is None
            assert await session.get(EmailAttachment, shared_attachment_b.id) is not None
            assert await session.get(OssObject, shared_object.id) is not None
            assert len(physically_deleted) == shared_deleted_before

            # 2) Email aggregate deletion removes raw EML and attachment metadata.
            raw_object = OssObject(
                bucket="delete-e2e-bucket",
                object_key=f"{batch}/raw.eml",
                source_type="delete_e2e",
                upload_status="success",
            )
            email_attachment_object = OssObject(
                bucket="delete-e2e-bucket",
                object_key=f"{batch}/email-attachment.bin",
                source_type="delete_e2e",
                upload_status="success",
            )
            session.add_all([raw_object, email_attachment_object])
            await session.flush()
            created_object_ids.extend([raw_object.id, email_attachment_object.id])
            email = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-email@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
                raw_eml_oss_object_id=raw_object.id,
            )
            session.add(email)
            await session.flush()
            created_email_ids.append(email.id)
            email_attachment = EmailAttachment(
                email_id=email.id,
                oss_object_id=email_attachment_object.id,
                file_name="email-attachment.bin",
                parse_status="pending",
            )
            session.add(email_attachment)
            await session.commit()

            preview = await deletions.preview_email(session, email.id, int(user_id))
            assert preview["deletable"] is True
            result = await deletions.delete_email(
                session,
                email_id=email.id,
                user_id=int(user_id),
                reason=batch,
                confirmation_token=preview["confirmation_token"],
            )
            audit_ids.append(result["deletion_operation_id"])
            email_delete_job = await session.get(JobRunLog, result["job_id"])
            assert email_delete_job is not None
            assert email_delete_job.status == "success"
            assert email_delete_job.success_count == 2
            assert await session.get(Email, email.id) is None
            assert await session.get(EmailAttachment, email_attachment.id) is None
            assert await session.get(OssObject, raw_object.id) is None
            assert await session.get(OssObject, email_attachment_object.id) is None
            with pytest.raises(deletions.DeletionError) as missing_email:
                await deletions.preview_email(session, email.id, int(user_id))
            assert missing_email.value.status_code == 404

            thread = EmailThread(
                thread_key=f"{batch}-thread",
                normalized_subject=batch,
                email_count=0,
            )
            session.add(thread)
            await session.flush()
            created_thread_ids.append(thread.id)
            thread_first = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-thread-1@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
                thread_id=thread.id,
            )
            thread_latest = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-thread-2@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
                thread_id=thread.id,
            )
            session.add_all([thread_first, thread_latest])
            await session.flush()
            created_email_ids.extend([thread_first.id, thread_latest.id])
            thread.root_message_id = thread_first.message_id
            thread.latest_email_id = thread_latest.id
            thread.email_count = 2
            await session.commit()

            preview = await deletions.preview_email(session, thread_latest.id, int(user_id))
            result = await deletions.delete_email(
                session,
                email_id=thread_latest.id,
                user_id=int(user_id),
                reason=batch,
                confirmation_token=preview["confirmation_token"],
            )
            audit_ids.append(result["deletion_operation_id"])
            retained_thread = await session.get(EmailThread, thread.id)
            assert retained_thread is not None
            assert retained_thread.latest_email_id == thread_first.id
            assert retained_thread.root_message_id == thread_first.message_id
            assert retained_thread.email_count == 1

            preview = await deletions.preview_email(session, thread_first.id, int(user_id))
            result = await deletions.delete_email(
                session,
                email_id=thread_first.id,
                user_id=int(user_id),
                reason=batch,
                confirmation_token=preview["confirmation_token"],
            )
            audit_ids.append(result["deletion_operation_id"])
            assert await session.get(EmailThread, thread.id) is None

            # A database commit failure rolls back business rows and never calls OSS.
            rollback_email = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-rollback@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
            )
            rollback_object = OssObject(
                bucket="delete-e2e-bucket",
                object_key=f"{batch}/rollback.bin",
                source_type="delete_e2e",
                upload_status="success",
            )
            session.add_all([rollback_email, rollback_object])
            await session.flush()
            created_email_ids.append(rollback_email.id)
            created_object_ids.append(rollback_object.id)
            rollback_attachment = EmailAttachment(
                email_id=rollback_email.id,
                oss_object_id=rollback_object.id,
                file_name="rollback.bin",
                parse_status="pending",
            )
            session.add(rollback_attachment)
            await session.commit()
            rollback_attachment_id = rollback_attachment.id
            rollback_object_id = rollback_object.id
            preview = await deletions.preview_attachment(session, rollback_attachment.id, int(user_id))
            deleted_before_failure = len(physically_deleted)

            def fail_next_commit(_session) -> None:
                raise RuntimeError("DELETE_E2E_COMMIT_FAILURE")

            event.listen(session.sync_session, "before_commit", fail_next_commit, once=True)
            with pytest.raises(RuntimeError, match="DELETE_E2E_COMMIT_FAILURE"):
                await deletions.delete_attachment(
                    session,
                    attachment_id=rollback_attachment.id,
                    user_id=int(user_id),
                    reason=batch,
                    confirmation_token=preview["confirmation_token"],
                )
            await session.rollback()
            assert await session.get(EmailAttachment, rollback_attachment_id) is not None
            assert await session.get(OssObject, rollback_object_id) is not None
            assert len(physically_deleted) == deleted_before_failure

            # 3) DB commit survives an OSS outage and a retry completes cleanup.
            retry_email = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-retry@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
            )
            retry_object = OssObject(
                bucket="delete-e2e-bucket",
                object_key=f"{batch}/retry.bin",
                source_type="delete_e2e",
                upload_status="success",
            )
            session.add_all([retry_email, retry_object])
            await session.flush()
            created_email_ids.append(retry_email.id)
            created_object_ids.append(retry_object.id)
            retry_attachment = EmailAttachment(
                email_id=retry_email.id,
                oss_object_id=retry_object.id,
                file_name="retry.bin",
                parse_status="pending",
            )
            session.add(retry_attachment)
            await session.commit()
            fail_once.add(retry_object.object_key)

            preview = await deletions.preview_attachment(session, retry_attachment.id, int(user_id))
            result = await deletions.delete_attachment(
                session,
                attachment_id=retry_attachment.id,
                user_id=int(user_id),
                reason=batch,
                confirmation_token=preview["confirmation_token"],
            )
            audit_ids.append(result["deletion_operation_id"])
            assert result["database_status"] == "deleted"
            assert result["oss_status"] == "pending"
            assert await session.get(EmailAttachment, retry_attachment.id) is None
            assert await session.get(OssObject, retry_object.id) is not None

            retry_result = await deletions.process_oss_deletion_operation(
                session, result["deletion_operation_id"]
            )
            await session.commit()
            assert retry_result["status"] == "success"
            assert await session.get(OssObject, retry_object.id) is None

            attachment_ai_email = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-attachment-ai@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
            )
            session.add(attachment_ai_email)
            await session.flush()
            created_email_ids.append(attachment_ai_email.id)
            attachment_ai_file = EmailAttachment(
                email_id=attachment_ai_email.id,
                file_name="ai-source.txt",
                parse_status="pending",
            )
            session.add(attachment_ai_file)
            await session.flush()
            attachment_ai = AiCallLog(
                trace_id=f"{batch}-attachment-ai",
                attachment_id=attachment_ai_file.id,
                call_type="delete_e2e",
                model_name="mock",
                prompt_version="v1",
                log_file_path="",
                status="success",
            )
            session.add(attachment_ai)
            await session.commit()
            created_ai_ids.append(attachment_ai.id)
            preview = await deletions.preview_email(session, attachment_ai_email.id, int(user_id))
            assert preview["deletable"] is True
            assert preview["affected_counts"]["ai_call_logs"] == 1
            result = await deletions.delete_email(
                session,
                email_id=attachment_ai_email.id,
                user_id=int(user_id),
                reason=batch,
                confirmation_token=preview["confirmation_token"],
            )
            audit_ids.append(result["deletion_operation_id"])
            assert await session.get(AiCallLog, attachment_ai.id) is None
            assert await session.get(Email, attachment_ai_email.id) is None

            sent_email = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-sent@invalid.local>",
                from_address="delete-e2e@invalid.local",
                mail_direction="outbound",
                parse_status="sent",
                subject=batch,
            )
            sent_object = OssObject(
                bucket="delete-e2e-bucket",
                object_key=f"{batch}/sent.pdf",
                source_type="delete_e2e",
                upload_status="success",
            )
            session.add_all([sent_email, sent_object])
            await session.flush()
            created_email_ids.append(sent_email.id)
            created_object_ids.append(sent_object.id)
            sent_attachment = EmailAttachment(
                email_id=sent_email.id,
                oss_object_id=sent_object.id,
                file_name="sent.pdf",
                parse_status="generated",
            )
            session.add(sent_attachment)
            await session.commit()
            protected_attachment_preview = await deletions.preview_attachment(
                session, sent_attachment.id, int(user_id)
            )
            assert protected_attachment_preview["deletable"] is False
            assert "ATTACHMENT_PARENT_EMAIL_ALREADY_SENT" in protected_attachment_preview["blockers"]

            # 4) Ticket deletion removes ticket-owned children and only detaches mail.
            source_email = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-ticket-source@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
            )
            related_email = Email(
                mailbox_account="delete-e2e@invalid.local",
                message_id=f"<{batch}-ticket-followup@invalid.local>",
                from_address="fixture@invalid.local",
                mail_direction="inbound",
                subject=batch,
            )
            session.add_all([source_email, related_email])
            await session.flush()
            created_email_ids.extend([source_email.id, related_email.id])
            ticket = RepairTicket(
                ticket_no=batch,
                current_status_code="new_email",
                source_email_id=source_email.id,
            )
            session.add(ticket)
            await session.flush()
            created_ticket_ids.append(ticket.id)
            item = RepairTicketItem(ticket_id=ticket.id, line_no=1, sn=f"{batch}-SN-1")
            second_item = RepairTicketItem(ticket_id=ticket.id, line_no=2, sn=f"{batch}-SN-2")
            link = EmailTicketLink(
                email_id=source_email.id,
                ticket_id=ticket.id,
                link_type="source",
                link_reason=batch,
            )
            related_link = EmailTicketLink(
                email_id=related_email.id,
                ticket_id=ticket.id,
                link_type="followup",
                link_reason=batch,
            )
            source_attachment = EmailAttachment(
                email_id=source_email.id,
                file_name="source-evidence.txt",
                parse_status="pending",
            )
            related_attachment = EmailAttachment(
                email_id=related_email.id,
                file_name="followup-evidence.txt",
                parse_status="pending",
            )
            session.add_all(
                [
                    item,
                    second_item,
                    link,
                    related_link,
                    source_attachment,
                    related_attachment,
                ]
            )
            running_job = JobRunLog(
                job_name="delete_e2e_running",
                job_type="parse_email",
                status="running",
                resource_type="repair_ticket",
                resource_id=ticket.id,
                idempotency_key=f"{batch}:running",
            )
            ticket_ai = AiCallLog(
                trace_id=f"{batch}-ticket-ai",
                ticket_id=ticket.id,
                call_type="delete_e2e",
                model_name="mock",
                prompt_version="v1",
                log_file_path="",
                status="success",
            )
            session.add_all([running_job, ticket_ai])
            await session.flush()
            created_ai_ids.append(ticket_ai.id)
            draft_reply = ReplyRecord(
                ticket_id=ticket.id,
                reply_type="missing_fields",
                to_addresses="nobody@invalid.local",
                ai_call_log_id=ticket_ai.id,
                send_status="draft",
                review_status="pending",
            )
            session.add(draft_reply)
            await session.commit()
            created_reply_ids.append(draft_reply.id)
            created_job_ids.append(running_job.id)

            preview = await deletions.preview_ticket(session, ticket.id, int(user_id))
            assert preview["deletable"] is False
            assert "TICKET_IN_ACTIVE_JOB" in preview["blockers"]
            with pytest.raises(deletions.DeletionError, match="DELETE_BLOCKED"):
                await deletions.delete_ticket(
                    session,
                    ticket_id=ticket.id,
                    user_id=int(user_id),
                    reason=batch,
                    confirmation_token=preview["confirmation_token"],
                )
            running_job.status = "success"
            await session.commit()

            preview = await deletions.preview_ticket(session, ticket.id, int(user_id))
            assert preview["deletable"] is True
            assert preview["affected_counts"]["items"] == 2
            assert preview["affected_counts"]["email_links"] == 2
            result = await deletions.delete_ticket(
                session,
                ticket_id=ticket.id,
                user_id=int(user_id),
                reason=batch,
                confirmation_token=preview["confirmation_token"],
            )
            audit_ids.append(result["deletion_operation_id"])
            assert await session.get(RepairTicket, ticket.id) is None
            assert await session.get(RepairTicketItem, item.id) is None
            assert await session.get(RepairTicketItem, second_item.id) is None
            assert await session.get(EmailTicketLink, link.id) is None
            assert await session.get(EmailTicketLink, related_link.id) is None
            assert await session.get(ReplyRecord, draft_reply.id) is None
            assert await session.get(AiCallLog, ticket_ai.id) is None
            assert await session.get(Email, source_email.id) is not None
            assert await session.get(Email, related_email.id) is not None
            assert await session.get(EmailAttachment, source_attachment.id) is not None
            assert await session.get(EmailAttachment, related_attachment.id) is not None
            with pytest.raises(deletions.DeletionError) as missing_ticket:
                await deletions.preview_ticket(session, ticket.id, int(user_id))
            assert missing_ticket.value.status_code == 404

            irreversible_ticket = RepairTicket(
                ticket_no=f"{batch}-irreversible",
                current_status_code="ready_for_export",
            )
            session.add(irreversible_ticket)
            await session.flush()
            created_ticket_ids.append(irreversible_ticket.id)
            irreversible_item = RepairTicketItem(
                ticket_id=irreversible_ticket.id,
                line_no=1,
                sn=f"{batch}-IRREVERSIBLE-SN",
            )
            irreversible_reply = ReplyRecord(
                ticket_id=irreversible_ticket.id,
                reply_type="rma",
                to_addresses="rmatest2@accotest.com",
                send_status="sent",
                review_status="approved",
            )
            irreversible_relay = TicketRelayExport(
                ticket_id=irreversible_ticket.id,
                ticket_version=1,
                payload_hash="a" * 64,
                payload_snapshot={"batch": batch},
                status="accepted",
            )
            session.add_all([irreversible_item, irreversible_reply, irreversible_relay])
            await session.flush()
            irreversible_export = ExportSap(
                ticket_id=irreversible_ticket.id,
                ticket_item_id=irreversible_item.id,
                relay_export_id=irreversible_relay.id,
                ticket_version=1,
                source_request_id=f"{batch}-submission",
                payload_hash="b" * 64,
                status="accepted",
                remote_call_id=f"{batch}-call",
                sn=irreversible_item.sn,
            )
            irreversible_rma = TicketRma(
                ticket_id=irreversible_ticket.id,
                rma_no=f"{batch}-RMA",
                status="sent",
                reply_record_id=irreversible_reply.id,
            )
            irreversible_operation = ExternalOperationRecord(
                operation_type="relay_insert",
                operation_key=f"{batch}-uncertain",
                status="uncertain",
                ticket_id=irreversible_ticket.id,
                retryable=True,
            )
            session.add_all([irreversible_export, irreversible_rma, irreversible_operation])
            await session.commit()
            irreversible_preview = await deletions.preview_ticket(
                session, irreversible_ticket.id, int(user_id)
            )
            assert irreversible_preview["deletable"] is False
            assert set(irreversible_preview["blockers"]) == {
                "TICKET_EXTERNAL_OPERATION_ACTIVE_OR_UNCERTAIN",
                "TICKET_EXTERNAL_EFFECT_ALREADY_EXECUTED",
            }
            assert irreversible_preview["irreversible_effects"]["reply_ids"] == [
                irreversible_reply.id
            ]
            assert irreversible_preview["irreversible_effects"]["export_ids"] == [
                irreversible_export.id
            ]
            assert irreversible_preview["irreversible_effects"]["rma_ids"] == [
                irreversible_rma.id
            ]
            with pytest.raises(deletions.DeletionError, match="DELETE_BLOCKED"):
                await deletions.delete_ticket(
                    session,
                    ticket_id=irreversible_ticket.id,
                    user_id=int(user_id),
                    reason=batch,
                    confirmation_token=irreversible_preview["confirmation_token"],
                )
            await session.rollback()

            protected_ticket = RepairTicket(
                ticket_no=f"{batch}-protected",
                current_status_code="rma_sent",
            )
            session.add(protected_ticket)
            await session.commit()
            created_ticket_ids.append(protected_ticket.id)
            protected_ticket_id = protected_ticket.id
            protected_preview = await deletions.preview_ticket(
                session, protected_ticket_id, int(user_id)
            )
            assert protected_preview["deletable"] is False
            assert protected_preview["blockers"] == ["TICKET_EXTERNAL_EFFECT_ALREADY_EXECUTED"]
            original_app_env = settings.APP_ENV
            monkeypatch.setattr(settings, "APP_ENV", "production")
            with pytest.raises(deletions.DeletionError) as production_force:
                await deletions.delete_ticket(
                    session,
                    ticket_id=protected_ticket_id,
                    user_id=int(user_id),
                    reason=batch,
                    confirmation_token=protected_preview["confirmation_token"],
                    force_local_cleanup=True,
                )
            assert production_force.value.code == "LOCAL_FORCE_DELETE_NOT_ALLOWED"
            await session.rollback()
            assert await session.get(RepairTicket, protected_ticket_id) is not None

            monkeypatch.setattr(settings, "APP_ENV", original_app_env)
            protected_preview = await deletions.preview_ticket(
                session, protected_ticket_id, int(user_id)
            )
            forced = await deletions.delete_ticket(
                session,
                ticket_id=protected_ticket_id,
                user_id=int(user_id),
                reason=batch,
                confirmation_token=protected_preview["confirmation_token"],
                force_local_cleanup=True,
            )
            audit_ids.append(forced["deletion_operation_id"])
            assert forced["external_effects_not_reverted"] is True
            assert await session.get(RepairTicket, protected_ticket_id) is None

            audits = list(
                (
                    await session.execute(
                        select(OperationLog).where(OperationLog.id.in_(audit_ids))
                    )
                ).scalars()
            )
            assert {row.operation_type for row in audits} == {
                "attachment_deleted",
                "email_deleted",
                "ticket_deleted",
            }
            assert all(row.before_data and row.after_data for row in audits)
            assert len(physically_deleted) == 4
        finally:
            await session.rollback()
            if audit_ids:
                delete_job_ids = list(
                    (
                        await session.execute(
                            select(JobRunLog.id).where(
                                JobRunLog.resource_type == "operation_log",
                                JobRunLog.resource_id.in_(audit_ids),
                            )
                        )
                    ).scalars()
                )
                await session.execute(
                    delete(ExternalOperationRecord).where(
                        or_(
                            *[
                                ExternalOperationRecord.operation_key.like(f"delete:{audit_id}:%")
                                for audit_id in audit_ids
                            ]
                        )
                    )
                )
                await session.execute(
                    delete(SystemEventLog).where(
                        SystemEventLog.job_run_id.in_(delete_job_ids or [-1])
                    )
                )
                await session.execute(
                    delete(JobRunLog).where(
                        JobRunLog.resource_type == "operation_log",
                        JobRunLog.resource_id.in_(audit_ids),
                    )
                )
                await session.execute(delete(OperationLog).where(OperationLog.id.in_(audit_ids)))
            await session.execute(delete(EmailTicketLink).where(EmailTicketLink.email_id.in_(created_email_ids or [-1])))
            await session.execute(delete(JobRunLog).where(JobRunLog.id.in_(created_job_ids or [-1])))
            await session.execute(delete(ExternalOperationRecord).where(ExternalOperationRecord.ticket_id.in_(created_ticket_ids or [-1])))
            await session.execute(delete(TicketRma).where(TicketRma.ticket_id.in_(created_ticket_ids or [-1])))
            await session.execute(delete(ExportSap).where(ExportSap.ticket_id.in_(created_ticket_ids or [-1])))
            await session.execute(delete(TicketRelayExport).where(TicketRelayExport.ticket_id.in_(created_ticket_ids or [-1])))
            await session.execute(delete(ReplyRecord).where(ReplyRecord.id.in_(created_reply_ids or [-1])))
            await session.execute(delete(ReplyRecord).where(ReplyRecord.ticket_id.in_(created_ticket_ids or [-1])))
            await session.execute(delete(AiCallLog).where(AiCallLog.id.in_(created_ai_ids or [-1])))
            await session.execute(delete(RepairTicketItem).where(RepairTicketItem.ticket_id.in_(created_ticket_ids or [-1])))
            await session.execute(delete(RepairTicket).where(RepairTicket.id.in_(created_ticket_ids or [-1])))
            await session.execute(delete(EmailAttachment).where(EmailAttachment.email_id.in_(created_email_ids or [-1])))
            await session.execute(delete(Email).where(Email.id.in_(created_email_ids or [-1])))
            await session.execute(delete(EmailThread).where(EmailThread.id.in_(created_thread_ids or [-1])))
            await session.execute(delete(OssObject).where(OssObject.id.in_(created_object_ids or [-1])))
            await session.commit()
