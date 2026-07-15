from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import get_client_ip, get_correlation_id, get_user_agent
from app.models import NotificationEvent, OperationLog, SystemEventLog
from app.services.logging_safety import sanitize_log_payload


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
    correlation_id: str | None = None,
    email_id: int | None = None,
    ticket_id: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> OperationLog:
    log = OperationLog(
        user_id=user_id,
        correlation_id=correlation_id or get_correlation_id(),
        email_id=email_id,
        ticket_id=ticket_id,
        operation_type=operation_type,
        target_type=target_type,
        target_id=target_id,
        description=description,
        before_data=sanitize_log_payload(before_data) if before_data else None,
        after_data=sanitize_log_payload(after_data) if after_data else None,
        ip_address=ip_address or get_client_ip(),
        user_agent=user_agent or get_user_agent(),
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
    event_stage: str | None = None,
    event_status: str | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
) -> SystemEventLog:
    event = SystemEventLog(
        event_type=event_type,
        severity=severity,
        module_name=module_name,
        correlation_id=correlation_id or get_correlation_id(),
        email_id=email_id,
        ticket_id=ticket_id,
        job_run_id=job_run_id,
        event_stage=event_stage,
        event_status=event_status,
        target_type=target_type,
        target_id=target_id,
        duration_ms=duration_ms,
        error_code=error_code,
        message=message,
        details=sanitize_log_payload(details) if details else None,
        stack_trace=(stack_trace or "")[:4000] or None,
    )
    session.add(event)
    return event
