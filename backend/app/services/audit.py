from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotificationEvent, OperationLog, SystemEventLog


async def log_operation(
    session: AsyncSession,
    *,
    operation_type: str,
    target_type: str,
    target_id: int | None = None,
    user_id: int | None = None,
    description: str | None = None,
    before_data: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
) -> OperationLog:
    log = OperationLog(
        user_id=user_id,
        operation_type=operation_type,
        target_type=target_type,
        target_id=target_id,
        description=description,
        before_data=before_data,
        after_data=after_data,
    )
    session.add(log)
    return log


async def create_notification(
    session: AsyncSession,
    *,
    event_type: str,
    target_type: str,
    target_id: int,
    title: str,
    content: str | None = None,
    priority: str = "normal",
    recipient_user_id: int | None = None,
    recipient_role_code: str | None = "operator",
    metadata: dict[str, Any] | None = None,
) -> NotificationEvent:
    event = NotificationEvent(
        event_type=event_type,
        target_type=target_type,
        target_id=target_id,
        title=title,
        content=content,
        priority=priority,
        recipient_user_id=recipient_user_id,
        recipient_role_code=recipient_role_code,
        metadata_json=metadata,
    )
    session.add(event)
    return event


async def log_system_event(
    session: AsyncSession,
    *,
    event_type: str,
    module_name: str,
    message: str,
    severity: str = "info",
    correlation_id: str | None = None,
    email_id: int | None = None,
    ticket_id: int | None = None,
    job_run_id: int | None = None,
    details: dict[str, Any] | None = None,
    stack_trace: str | None = None,
) -> SystemEventLog:
    event = SystemEventLog(
        event_type=event_type,
        severity=severity,
        module_name=module_name,
        correlation_id=correlation_id,
        email_id=email_id,
        ticket_id=ticket_id,
        job_run_id=job_run_id,
        message=message,
        details=details,
        stack_trace=stack_trace,
    )
    session.add(event)
    return event
