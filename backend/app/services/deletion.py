"""邮件 / 工单 / 附件删除 Service。

删除遵循以下原则：
- 单事务内按 FK 依赖顺序清理，Service 层自己 ``await session.commit()``。
- ``force=False`` 时先做业务状态检查，命中活跃引用或外部系统已同步则拒绝。
- 主事务提交后再清理 OSS（DB 记录 + 远程对象），OSS 失败不阻断整体删除。
- 审计日志只记录非敏感元数据（标题、地址、message_id、文件名、状态码），不含正文/二进制/签名 URL。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiCallLog,
    Email,
    EmailAttachment,
    EmailThread,
    EmailTicketLink,
    ExportSap,
    ExternalOperationRecord,
    FieldAuditLog,
    MailFetchRecord,
    ManualReviewTask,
    NotificationEvent,
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
from app.services.storage import delete_oss_object, delete_oss_objects_batch

logger = logging.getLogger(__name__)


async def _cleanup_oss_objects(
    session: AsyncSession,
    oss_object_ids: list[int],
) -> tuple[int, list[dict[str, Any]]]:
    """主事务提交后批量清理 OSS 对象。

    每个 OSS 对象独立提交/回滚，单条失败不阻断后续清理。返回 (成功数, 逐条结果)。
    """
    cleaned = 0
    results: list[dict[str, Any]] = []
    for oid in oss_object_ids:
        if oid is None:
            continue
        try:
            result = await delete_oss_object(
                session,
                oss_object_id=oid,
                hard_delete_oss=True,
                force=True,
            )
            await session.commit()
            is_deleted = bool(isinstance(result, dict) and result.get("deleted"))
            if is_deleted:
                cleaned += 1
            results.append({"oss_object_id": oid, "deleted": is_deleted, "result": result})
        except Exception as exc:  # noqa: BLE001 - OSS 清理失败不阻断删除
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("清理 OSS 对象 %s 失败: %s", oid, exc)
            results.append({"oss_object_id": oid, "deleted": False, "error": str(exc)})
    return cleaned, results


async def delete_attachment(
    session: AsyncSession,
    attachment_id: int,
    operator_user_id: int,
    force: bool = False,
) -> dict[str, Any]:
    """删除邮件附件，级联清理 ParseResult / AiCallLog，提交后清理 OSS。"""
    attachment = await session.get(EmailAttachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ATTACHMENT_NOT_FOUND")

    # force=False 时检查引用
    if not force:
        parse_count = await session.scalar(
            select(func.count())
            .select_from(ParseResult)
            .where(ParseResult.source_attachment_id == attachment_id)
        )
        ai_count = await session.scalar(
            select(func.count())
            .select_from(AiCallLog)
            .where(AiCallLog.attachment_id == attachment_id)
        )
        if (parse_count or 0) > 0 or (ai_count or 0) > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="ATTACHMENT_IN_USE",
            )

    oss_object_id = attachment.oss_object_id

    # 按 FK 依赖顺序删除
    cascaded: dict[str, int] = {}

    result = await session.execute(
        delete(ParseResult).where(ParseResult.source_attachment_id == attachment_id)
    )
    cascaded["parse_results"] = result.rowcount

    result = await session.execute(
        delete(AiCallLog).where(AiCallLog.attachment_id == attachment_id)
    )
    cascaded["ai_call_logs"] = result.rowcount

    result = await session.execute(
        delete(EmailAttachment).where(EmailAttachment.id == attachment_id)
    )
    cascaded["email_attachment"] = result.rowcount

    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="delete_attachment",
        target_type="email_attachment",
        target_id=attachment_id,
        description=f"删除附件: {attachment.file_name}",
        before_data={
            "file_name": attachment.file_name,
            "file_size": attachment.file_size,
            "email_id": attachment.email_id,
        },
        after_data={
            "deleted": True,
            "force": force,
            "cascaded": cascaded,
        },
    )
    await session.commit()

    # 提交后清理 OSS（不阻断）
    oss_result: dict[str, Any] | None = None
    if oss_object_id is not None:
        _, oss_results = await _cleanup_oss_objects(session, [oss_object_id])
        oss_result = oss_results[0] if oss_results else None

    return {
        "deleted": True,
        "attachment_id": attachment_id,
        "email_id": attachment.email_id,
        "oss_object_id": oss_object_id,
        "oss_result": oss_result,
        "cascaded": cascaded,
    }


async def delete_email(
    session: AsyncSession,
    email_id: int,
    operator_user_id: int,
    force: bool = False,
) -> dict[str, Any]:
    """删除邮件及其全部子表，提交后批量清理附件 OSS。"""
    email = await session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")

    # force=False 时业务状态检查
    if not force:
        if email.processing_stage in ("fetched", "parsing", "classifying"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="EMAIL_BEING_PROCESSED",
            )
        if email.parse_status == "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="EMAIL_PARSE_PENDING",
            )

    subject = email.subject
    from_address = email.from_address
    message_id = email.message_id

    # 收集附件 oss_object_ids（提交后清理）
    oss_object_ids = list(
        (
            await session.execute(
                select(EmailAttachment.oss_object_id).where(
                    EmailAttachment.email_id == email_id,
                    EmailAttachment.oss_object_id.isnot(None),
                )
            )
        ).scalars().all()
    )

    # 按 FK 依赖顺序删除（15 步）
    cascaded: dict[str, int] = {}

    result = await session.execute(delete(AiCallLog).where(AiCallLog.email_id == email_id))
    cascaded["ai_call_logs"] = result.rowcount

    result = await session.execute(delete(OperationLog).where(OperationLog.email_id == email_id))
    cascaded["operation_logs"] = result.rowcount

    result = await session.execute(delete(SystemEventLog).where(SystemEventLog.email_id == email_id))
    cascaded["system_event_logs"] = result.rowcount

    result = await session.execute(
        delete(ExternalOperationRecord).where(ExternalOperationRecord.email_id == email_id)
    )
    cascaded["external_operation_records"] = result.rowcount

    result = await session.execute(delete(MailFetchRecord).where(MailFetchRecord.email_id == email_id))
    cascaded["mail_fetch_records"] = result.rowcount

    result = await session.execute(delete(ManualReviewTask).where(ManualReviewTask.email_id == email_id))
    cascaded["manual_review_tasks"] = result.rowcount

    result = await session.execute(
        delete(ReplyRecord).where(
            or_(
                ReplyRecord.related_email_id == email_id,
                ReplyRecord.outgoing_email_id == email_id,
            )
        )
    )
    cascaded["reply_records"] = result.rowcount

    result = await session.execute(delete(ParseResult).where(ParseResult.email_id == email_id))
    cascaded["parse_results"] = result.rowcount

    result = await session.execute(
        delete(EmailAttachment).where(EmailAttachment.email_id == email_id)
    )
    cascaded["email_attachments"] = result.rowcount

    result = await session.execute(
        delete(EmailTicketLink).where(EmailTicketLink.email_id == email_id)
    )
    cascaded["email_ticket_links"] = result.rowcount

    # 解除自引用
    result = await session.execute(
        update(Email)
        .values(duplicate_of_email_id=None)
        .where(Email.duplicate_of_email_id == email_id)
    )
    cascaded["duplicate_emails_detached"] = result.rowcount

    result = await session.execute(
        update(EmailThread)
        .values(latest_email_id=None)
        .where(EmailThread.latest_email_id == email_id)
    )
    cascaded["email_threads_latest_detached"] = result.rowcount

    result = await session.execute(
        update(RepairTicket)
        .values(source_email_id=None)
        .where(RepairTicket.source_email_id == email_id)
    )
    cascaded["repair_tickets_source_detached"] = result.rowcount

    result = await session.execute(
        update(RepairTicket)
        .values(device_received_email_id=None)
        .where(RepairTicket.device_received_email_id == email_id)
    )
    cascaded["repair_tickets_device_received_detached"] = result.rowcount

    result = await session.execute(delete(Email).where(Email.id == email_id))
    cascaded["email"] = result.rowcount

    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="delete_email",
        target_type="email",
        target_id=email_id,
        description=f"删除邮件: {subject or ''}",
        before_data={
            "subject": subject,
            "from_address": from_address,
            "message_id": message_id,
        },
        after_data={
            "deleted": True,
            "force": force,
            "cascaded": cascaded,
        },
    )
    await session.commit()

    # 提交后批量清理附件 OSS（不阻断）
    oss_objects_cleaned, oss_results = await _cleanup_oss_objects(session, oss_object_ids)

    return {
        "deleted": True,
        "email_id": email_id,
        "oss_objects_cleaned": oss_objects_cleaned,
        "oss_results": oss_results,
        "cascaded": cascaded,
    }


async def delete_ticket(
    session: AsyncSession,
    ticket_id: int,
    operator_user_id: int,
    force: bool = False,
) -> dict[str, Any]:
    """删除工单及其全部子表，提交后批量清理 RMA PDF OSS。"""
    ticket = await session.get(RepairTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")

    # force=False 时业务状态检查
    if not force:
        if ticket.current_status_code in ("ready_for_export", "rma_sent"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="TICKET_IN_EXTERNAL_SYSTEM",
            )
        if ticket.rma_status in ("issued", "sent"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="RMA_ALREADY_ISSUED",
            )
        if ticket.relay_export_status in ("exported", "accepted"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="SAP_ALREADY_SYNCED",
            )
        sent_reply_count = await session.scalar(
            select(func.count())
            .select_from(ReplyRecord)
            .where(
                ReplyRecord.ticket_id == ticket_id,
                ReplyRecord.send_status == "sent",
            )
        )
        if sent_reply_count and sent_reply_count > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="EMAIL_ALREADY_SENT",
            )

    ticket_no = ticket.ticket_no
    current_status_code = ticket.current_status_code
    ticket_version = ticket.version

    # 预收集 RMA id 列表 + pdf_oss_object_id（TicketRmaItem 依赖 ticket_rma_id）
    rma_rows = (
        await session.execute(
            select(TicketRma.id, TicketRma.pdf_oss_object_id).where(
                TicketRma.ticket_id == ticket_id
            )
        )
    ).all()
    rma_ids = [row[0] for row in rma_rows]
    rma_pdf_oss_object_ids = [row[1] for row in rma_rows if row[1] is not None]

    # 预收集 reply_records 的 rma_pdf_oss_object_id
    reply_pdf_oss_object_ids = list(
        (
            await session.execute(
                select(ReplyRecord.rma_pdf_oss_object_id).where(
                    ReplyRecord.ticket_id == ticket_id,
                    ReplyRecord.rma_pdf_oss_object_id.isnot(None),
                )
            )
        ).scalars().all()
    )

    # 合并去重 OSS id 列表（提交后清理）
    oss_object_ids = list({*rma_pdf_oss_object_ids, *reply_pdf_oss_object_ids})

    # 按 FK 依赖顺序删除（20 步）
    cascaded: dict[str, int] = {}

    result = await session.execute(delete(AiCallLog).where(AiCallLog.ticket_id == ticket_id))
    cascaded["ai_call_logs"] = result.rowcount

    result = await session.execute(delete(OperationLog).where(OperationLog.ticket_id == ticket_id))
    cascaded["operation_logs"] = result.rowcount

    result = await session.execute(delete(SystemEventLog).where(SystemEventLog.ticket_id == ticket_id))
    cascaded["system_event_logs"] = result.rowcount

    result = await session.execute(delete(ParseResult).where(ParseResult.ticket_id == ticket_id))
    cascaded["parse_results"] = result.rowcount

    result = await session.execute(
        delete(SnValidationResult).where(SnValidationResult.ticket_id == ticket_id)
    )
    cascaded["sn_validation_results"] = result.rowcount

    # 不删邮件，只解除关联
    result = await session.execute(
        delete(EmailTicketLink).where(EmailTicketLink.ticket_id == ticket_id)
    )
    cascaded["email_ticket_links"] = result.rowcount

    result = await session.execute(
        update(NotificationEvent)
        .values(ticket_id=None)
        .where(NotificationEvent.ticket_id == ticket_id)
    )
    cascaded["notification_events_detached"] = result.rowcount

    result = await session.execute(
        update(EmailThread)
        .values(ticket_id=None)
        .where(EmailThread.ticket_id == ticket_id)
    )
    cascaded["email_threads_ticket_detached"] = result.rowcount

    result = await session.execute(
        update(EmailThread)
        .values(predecessor_ticket_id=None)
        .where(EmailThread.predecessor_ticket_id == ticket_id)
    )
    cascaded["email_threads_predecessor_detached"] = result.rowcount

    result = await session.execute(
        delete(TicketStatusLog).where(TicketStatusLog.ticket_id == ticket_id)
    )
    cascaded["ticket_status_logs"] = result.rowcount

    result = await session.execute(
        delete(FieldAuditLog).where(FieldAuditLog.ticket_id == ticket_id)
    )
    cascaded["field_audit_logs"] = result.rowcount

    result = await session.execute(delete(TicketRma).where(TicketRma.ticket_id == ticket_id))
    cascaded["ticket_rmas"] = result.rowcount

    if rma_ids:
        result = await session.execute(
            delete(TicketRmaItem).where(TicketRmaItem.ticket_rma_id.in_(rma_ids))
        )
        cascaded["ticket_rma_items"] = result.rowcount
    else:
        cascaded["ticket_rma_items"] = 0

    result = await session.execute(
        delete(TicketRelayExport).where(TicketRelayExport.ticket_id == ticket_id)
    )
    cascaded["ticket_relay_exports"] = result.rowcount

    result = await session.execute(delete(ExportSap).where(ExportSap.ticket_id == ticket_id))
    cascaded["export_sap"] = result.rowcount

    result = await session.execute(
        delete(ExternalOperationRecord).where(ExternalOperationRecord.ticket_id == ticket_id)
    )
    cascaded["external_operation_records"] = result.rowcount

    result = await session.execute(
        delete(ManualReviewTask).where(ManualReviewTask.ticket_id == ticket_id)
    )
    cascaded["manual_review_tasks"] = result.rowcount

    result = await session.execute(
        delete(ReplyRecord).where(ReplyRecord.ticket_id == ticket_id)
    )
    cascaded["reply_records"] = result.rowcount

    result = await session.execute(
        delete(RepairTicketItem).where(RepairTicketItem.ticket_id == ticket_id)
    )
    cascaded["repair_ticket_items"] = result.rowcount

    # 乐观锁删除主记录
    result = await session.execute(
        delete(RepairTicket).where(
            RepairTicket.id == ticket_id,
            RepairTicket.version == ticket_version,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="TICKET_VERSION_CONFLICT",
        )
    cascaded["repair_ticket"] = result.rowcount

    await log_operation(
        session,
        user_id=operator_user_id,
        operation_type="delete_ticket",
        target_type="repair_ticket",
        target_id=ticket_id,
        description=f"删除工单: {ticket_no}",
        before_data={
            "ticket_no": ticket_no,
            "current_status_code": current_status_code,
            "version": ticket_version,
        },
        after_data={
            "deleted": True,
            "force": force,
            "cascaded": cascaded,
        },
    )
    await session.commit()

    # 提交后批量清理 RMA PDF OSS（不阻断）
    oss_objects_cleaned, oss_results = await _cleanup_oss_objects(session, oss_object_ids)

    return {
        "deleted": True,
        "ticket_id": ticket_id,
        "ticket_no": ticket_no,
        "oss_objects_cleaned": oss_objects_cleaned,
        "oss_results": oss_results,
        "cascaded": cascaded,
    }


async def delete_emails_batch(
    session: AsyncSession,
    email_ids: list[int],
    operator_user_id: int,
    force: bool = False,
) -> list[dict[str, Any]]:
    """批量删除邮件，单条失败记录到结果中不阻断。"""
    results: list[dict[str, Any]] = []
    for eid in email_ids:
        try:
            results.append(
                await delete_email(session, eid, operator_user_id, force=force)
            )
        except HTTPException as exc:
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            results.append(
                {
                    "deleted": False,
                    "error": str(exc.detail),
                    "id": eid,
                    "status_code": exc.status_code,
                }
            )
        except Exception as exc:  # noqa: BLE001
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            results.append({"deleted": False, "error": str(exc), "id": eid})
    return results


async def delete_tickets_batch(
    session: AsyncSession,
    ticket_ids: list[int],
    operator_user_id: int,
    force: bool = False,
) -> list[dict[str, Any]]:
    """批量删除工单，单条失败记录到结果中不阻断。"""
    results: list[dict[str, Any]] = []
    for tid in ticket_ids:
        try:
            results.append(
                await delete_ticket(session, tid, operator_user_id, force=force)
            )
        except HTTPException as exc:
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            results.append(
                {
                    "deleted": False,
                    "error": str(exc.detail),
                    "id": tid,
                    "status_code": exc.status_code,
                }
            )
        except Exception as exc:  # noqa: BLE001
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            results.append({"deleted": False, "error": str(exc), "id": tid})
    return results


async def delete_attachments_batch(
    session: AsyncSession,
    attachment_ids: list[int],
    operator_user_id: int,
    force: bool = False,
) -> list[dict[str, Any]]:
    """批量删除附件，单条失败记录到结果中不阻断。"""
    results: list[dict[str, Any]] = []
    for aid in attachment_ids:
        try:
            results.append(
                await delete_attachment(session, aid, operator_user_id, force=force)
            )
        except HTTPException as exc:
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            results.append(
                {
                    "deleted": False,
                    "error": str(exc.detail),
                    "id": aid,
                    "status_code": exc.status_code,
                }
            )
        except Exception as exc:  # noqa: BLE001
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            results.append({"deleted": False, "error": str(exc), "id": aid})
    return results
