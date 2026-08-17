from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Email,
    EmailAttachment,
    EmailThread,
    ManualReviewTask,
    NotificationEvent,
    OssObject,
    RepairTicket,
    ReplyRecord,
    TicketRma,
    TicketStatusLog,
    WorkflowStatus,
    WorkflowTransition,
)
from app.services.audit import create_notification, log_operation
from app.services.common import utcnow
from app.services.notifications import resolve_notifications_for_ticket
from app.services.routing import choose_available_operator

OPEN_TASK_STATUSES = ("pending", "claimed", "assigned", "assignment_failed")


def _formal_rma_number(value: str | None) -> bool:
    normalized = (value or "").strip()
    if len(normalized) != 10 or not normalized.isdigit():
        return False
    try:
        datetime.strptime(normalized[:8], "%Y%m%d")
    except ValueError:
        return False
    return True


async def _rma_closure_missing_facts(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    metadata: dict[str, Any],
) -> list[str]:
    missing: list[str] = []
    reply_id = metadata.get("reply_id")
    reply = (
        await session.get(ReplyRecord, int(reply_id))
        if isinstance(reply_id, int) or (isinstance(reply_id, str) and reply_id.isdigit())
        else None
    )
    if reply is None or reply.ticket_id != ticket.id or reply.reply_type != "rma_authorization":
        return ["rma_reply_record"]
    if reply.send_status != "sent":
        missing.append("smtp_sent")
    if not reply.smtp_message_id:
        missing.append("smtp_message_id")
    if reply.archive_status != "archived" or reply.archive_verified_at is None:
        missing.append("reply_archive_verified")
    if not reply.outgoing_email_id:
        missing.append("outbound_email")
    if not reply.rma_pdf_oss_object_id:
        missing.append("rma_pdf_object")

    rma_record = await session.scalar(
        select(TicketRma).where(
            TicketRma.ticket_id == ticket.id,
            TicketRma.reply_record_id == reply.id,
        )
    )
    if rma_record is None:
        missing.append("ticket_rma")
    else:
        if not _formal_rma_number(rma_record.rma_no) or rma_record.received_at is None:
            missing.append("formal_rma_received")
        if (
            rma_record.pdf_validation_status != "passed"
            or not rma_record.pdf_sha256
            or rma_record.pdf_oss_object_id != reply.rma_pdf_oss_object_id
        ):
            missing.append("rma_pdf_validated")
        if rma_record.pdf_archive_status != "archived" or rma_record.pdf_archived_at is None:
            missing.append("rma_pdf_archived")
        if rma_record.status != "issued" or rma_record.issued_at is None:
            missing.append("rma_issued")

    outgoing = (
        await session.get(Email, reply.outgoing_email_id)
        if reply.outgoing_email_id
        else None
    )
    if (
        outgoing is None
        or outgoing.mail_direction != "outbound"
        or outgoing.raw_eml_oss_object_id is None
        or outgoing.message_id != reply.smtp_message_id
    ):
        missing.append("outbound_eml_archived")

    pdf_object = (
        await session.get(OssObject, reply.rma_pdf_oss_object_id)
        if reply.rma_pdf_oss_object_id
        else None
    )
    if pdf_object is None:
        missing.append("rma_pdf_oss_object")

    attachment = (
        await session.scalar(
            select(EmailAttachment).where(
                EmailAttachment.email_id == reply.outgoing_email_id,
                EmailAttachment.oss_object_id == reply.rma_pdf_oss_object_id,
            )
        )
        if reply.outgoing_email_id and reply.rma_pdf_oss_object_id
        else None
    )
    expected_hash = rma_record.pdf_sha256 if rma_record is not None else None
    if attachment is None or not expected_hash or attachment.file_hash != expected_hash:
        missing.append("outbound_rma_attachment")
    return sorted(set(missing))


def task_type_for_event(trigger_event: str) -> str:
    return {
        "parse_low_confidence": "parse_low_confidence",
        "field_conflict": "field_conflict",
        "manual_review_required": "manual",
        "system_error": "system_error",
        "followup_limit_exceeded": "followup_limit",
    }.get(trigger_event, "manual")


async def create_manual_task_if_missing(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    task_type: str,
    trigger_reason: str | None = None,
    priority: str = "normal",
    email_id: int | None = None,
    assigned_user_id: int | None = None,
    recovery_stage: str | None = None,
    recovery_action: str | None = None,
) -> ManualReviewTask:
    result = await session.execute(
        select(ManualReviewTask)
        .where(
            ManualReviewTask.ticket_id == ticket.id,
            ManualReviewTask.task_type == task_type,
            ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
        )
        .order_by(ManualReviewTask.created_at.desc())
    )
    existing = result.scalars().first()
    if existing is not None:
        existing.recovery_stage = existing.recovery_stage or recovery_stage or task_type
        existing.recovery_action = (
            existing.recovery_action
            or recovery_action
            or trigger_reason
            or "请核对异常原因并从对应业务阶段恢复。"
        )
        owner = await choose_available_operator(
            session,
            existing.assigned_user_id or assigned_user_id or ticket.assigned_user_id,
        )
        if owner is not None and (existing.status == "assignment_failed" or existing.assigned_user_id != owner.id):
            from app.services.notifications import resolve_notifications_for_target

            existing.status = "pending"
            existing.assigned_user_id = owner.id
            existing.claimed_by_user_id = None
            existing.claimed_at = None
            await resolve_notifications_for_target(
                session,
                target_type="manual_review_task",
                target_id=existing.id,
            )
            await create_notification(
                session,
                event_type="manual_review_assigned",
                target_type="manual_review_task",
                target_id=existing.id,
                title="人工复核任务已重新分配",
                content=trigger_reason or existing.trigger_reason,
                priority=priority,
                recipient_user_id=owner.id,
                recipient_role_code=None,
                metadata={"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "task_type": task_type},
            )
        return existing

    sticky_assignee = assigned_user_id or ticket.assigned_user_id
    owner = await choose_available_operator(session, sticky_assignee)
    task = ManualReviewTask(
        ticket_id=ticket.id,
        email_id=email_id or ticket.source_email_id,
        thread_id=getattr(ticket, "thread_id", None),
        task_type=task_type,
        priority=priority,
        status="pending",
        description=f"工单 {ticket.ticket_no} 需要人工复核。",
        trigger_reason=trigger_reason,
        recovery_stage=recovery_stage or task_type,
        recovery_action=(
            recovery_action
            or trigger_reason
            or "请核对异常原因并从对应业务阶段恢复。"
        ),
        assigned_user_id=owner.id if owner is not None else None,
    )
    session.add(task)
    await session.flush()
    if owner is not None:
        await create_notification(
            session,
            event_type="manual_review_assigned",
            target_type="manual_review_task",
            target_id=task.id,
            title="人工复核任务已由系统分配",
            content=trigger_reason or f"工单 {ticket.ticket_no} 需要处理。",
            priority=priority,
            recipient_user_id=owner.id,
            recipient_role_code=None,
            metadata={"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "task_type": task_type},
        )
    else:
        await create_notification(
            session,
            event_type="manual_review_assignment_failed",
            target_type="manual_review_task",
            target_id=task.id,
            title="人工复核任务负责人分配失败",
            content=f"工单 {ticket.ticket_no} 的系统负责人不可用，请管理员纠正负责人。",
            priority="high",
            recipient_user_id=None,
            recipient_role_code="admin",
            metadata={
                "ticket_id": ticket.id,
                "ticket_no": ticket.ticket_no,
                "task_type": task_type,
                "requested_owner_user_id": sticky_assignee,
            },
        )
    return task


async def create_email_manual_task_if_missing(
    session: AsyncSession,
    *,
    email: Email,
    task_type: str,
    trigger_reason: str,
    priority: str = "normal",
    recovery_stage: str = "email_classification",
    recovery_action: str | None = None,
) -> ManualReviewTask:
    existing = await session.scalar(
        select(ManualReviewTask)
        .where(
            ManualReviewTask.email_id == email.id,
            ManualReviewTask.task_type == task_type,
            ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
        )
        .order_by(ManualReviewTask.id.desc())
    )
    if existing is not None:
        return existing
    owner = await choose_available_operator(session, None)
    task = ManualReviewTask(
        ticket_id=None,
        email_id=email.id,
        thread_id=email.thread_id,
        task_type=task_type,
        priority=priority,
        status="pending" if owner is not None else "assignment_failed",
        description="邮件业务需要人工判断或通过现有业务渠道处理。",
        trigger_reason=trigger_reason,
        recovery_stage=recovery_stage,
        recovery_action=recovery_action or "人工定类、关联/创建工单或记录外部处理结果。",
        assigned_user_id=owner.id if owner is not None else None,
    )
    session.add(task)
    await session.flush()
    await create_notification(
        session,
        event_type="email_manual_business_assigned" if owner else "manual_review_assignment_failed",
        target_type="manual_review_task",
        target_id=task.id,
        title="邮件业务需要人工处理",
        content=trigger_reason,
        priority=priority,
        recipient_user_id=owner.id if owner else None,
        recipient_role_code=None if owner else "admin",
        metadata={"email_id": email.id, "thread_id": email.thread_id, "task_type": task_type},
        requires_attention=True,
    )
    return task


async def transition_ticket(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    to_status_code: str,
    trigger_event: str,
    user_id: int | None = None,
    operator_type: str = "user",
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    manual_task_type: str | None = None,
    manual_task_priority: str | None = None,
    resolving_task_id: int | None = None,
) -> RepairTicket:
    from_status_code = ticket.current_status_code
    if from_status_code in {"closed", "resolved"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="TICKET_ALREADY_TERMINAL")
    ticket_category = getattr(ticket, "ticket_category", "standard_repair")
    if ticket_category == "manual_business" and to_status_code in {"ready_for_export", "rma_sent", "closed"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="MANUAL_BUSINESS_RMA_TRANSITION_FORBIDDEN")
    if to_status_code == "resolved" and (
        ticket_category != "manual_business" or trigger_event != "manual_business_resolved"
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="MANUAL_BUSINESS_RESOLUTION_REQUIRED")
    if to_status_code == "ready_for_export" and not (metadata or {}).get("safety_check_hash"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="EXPORT_SAFETY_GATE_REQUIRED")
    if to_status_code == "rma_sent":
        evidence = metadata or {}
        if not evidence.get("reply_id") or not evidence.get("smtp_message_id"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="RMA_SMTP_EVIDENCE_REQUIRED",
            )
    if to_status_code == "closed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="DEVICE_INTAKE_CLOSURE_ENTRY_NOT_IMPLEMENTED",
        )
    if from_status_code == "manual_review" and to_status_code != "manual_review":
        blocker_query = select(ManualReviewTask.id).where(
            ManualReviewTask.ticket_id == ticket.id,
            ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
        )
        if to_status_code == "ready_for_export":
            # Return-route evidence is an RMA gate, not a SAP export field.
            # Keep its task open and visible without blocking a ticket whose
            # SN, policy, customer mailing fields and SAP payload are valid.
            blocker_query = blocker_query.where(
                ManualReviewTask.task_type != "return_route_review"
            )
        if resolving_task_id is not None:
            blocker_query = blocker_query.where(ManualReviewTask.id != resolving_task_id)
        blocker_id = await session.scalar(blocker_query.limit(1))
        if blocker_id is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="MANUAL_TASKS_UNRESOLVED")

    target_status = await session.scalar(
        select(WorkflowStatus).where(WorkflowStatus.status_code == to_status_code, WorkflowStatus.enabled == True)  # noqa: E712
    )
    if target_status is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="WORKFLOW_STATUS_NOT_FOUND")

    transition = await session.scalar(
        select(WorkflowTransition).where(
            WorkflowTransition.from_status_code == from_status_code,
            WorkflowTransition.to_status_code == to_status_code,
            WorkflowTransition.trigger_event == trigger_event,
            WorkflowTransition.enabled == True,  # noqa: E712
        )
    )
    if transition is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="WORKFLOW_TRANSITION_NOT_ALLOWED")

    ticket.current_status_code = to_status_code
    ticket.version += 1
    if target_status.is_terminal:
        if not trigger_event:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="TERMINAL_REASON_REQUIRED",
            )
        ticket.terminal_reason_code = trigger_event
        ticket.terminal_reason = reason or transition.condition_desc or trigger_event
        if to_status_code == "closed":
            ticket.closed_at = utcnow()
        elif to_status_code == "resolved":
            ticket.resolved_at = utcnow()
            ticket.resolution_code = trigger_event
            ticket.resolution_summary = reason or transition.condition_desc or trigger_event
    session.add(
        TicketStatusLog(
            ticket_id=ticket.id,
            from_status_code=from_status_code,
            to_status_code=to_status_code,
            trigger_event=trigger_event,
            reason=reason,
            operator_type=operator_type,
            operator_user_id=user_id,
            metadata_json=metadata,
        )
    )
    await log_operation(
        session,
        user_id=user_id,
        operation_type="ticket_transition",
        target_type="repair_ticket",
        target_id=ticket.id,
        description=reason,
        before_data={"status": from_status_code},
        after_data={"status": to_status_code, "trigger_event": trigger_event},
    )

    # A ticket-status notification is the source event for workflow states
    # that need attention but do not create a manual-review task themselves.
    if to_status_code == "need_customer_info":
        await create_notification(
            session,
            event_type="ticket_customer_info_required",
            target_type="repair_ticket",
            target_id=ticket.id,
            ticket_id=ticket.id,
            title="工单等待客户补充信息",
            content=reason or f"工单 {ticket.ticket_no} 缺少继续处理所需的客户信息。",
            priority="normal",
            recipient_user_id=ticket.assigned_user_id,
            recipient_role_code=None if ticket.assigned_user_id else "operator",
            metadata={"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "status": to_status_code},
            requires_attention=True,
        )
    elif to_status_code == "error":
        await create_notification(
            session,
            event_type="ticket_system_error",
            target_type="repair_ticket",
            target_id=ticket.id,
            ticket_id=ticket.id,
            title="工单处理发生系统异常",
            content=reason or f"工单 {ticket.ticket_no} 需要人工检查系统异常。",
            priority="high",
            recipient_user_id=ticket.assigned_user_id,
            recipient_role_code=None if ticket.assigned_user_id else "operator",
            metadata={"ticket_id": ticket.id, "ticket_no": ticket.ticket_no, "status": to_status_code},
            requires_attention=True,
        )

    if trigger_event == "customer_info_completed" or (
        from_status_code == "auto_replied" and to_status_code != "auto_replied"
    ):
        await resolve_notifications_for_ticket(
            session,
            ticket_id=ticket.id,
            event_types={"ticket_customer_info_required"},
        )
    if from_status_code == "error" and to_status_code != "error":
        await resolve_notifications_for_ticket(
            session,
            ticket_id=ticket.id,
            event_types={"ticket_system_error"},
        )
    if to_status_code in {"closed", "resolved"}:
        await resolve_notifications_for_ticket(session, ticket_id=ticket.id)
    if to_status_code == "closed" and ticket.thread_id:
        thread = await session.get(EmailThread, ticket.thread_id, with_for_update=True)
        if thread is not None and thread.ticket_id == ticket.id:
            thread.ticket_id = None

    if to_status_code == "manual_review":
        await create_manual_task_if_missing(
            session,
            ticket=ticket,
            task_type=manual_task_type or task_type_for_event(trigger_event),
            trigger_reason=reason or transition.condition_desc,
            priority=manual_task_priority or ("high" if trigger_event in {"system_error", "field_conflict"} else "normal"),
            email_id=ticket.source_email_id,
        )
    return ticket


async def mark_notification_read(session: AsyncSession, notification: NotificationEvent) -> NotificationEvent:
    notification.delivery_status = "read"
    notification.read_at = utcnow()
    notification.delivered_at = notification.delivered_at or notification.read_at
    return notification
