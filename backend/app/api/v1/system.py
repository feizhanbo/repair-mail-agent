from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.config import settings
from app.core.database import get_session
from app.core.response import ok
from app.models import AiCallLog, JobRunLog, MailFetchRecord, ReplyRecord, ReplyTemplate, SapSnSyncBatch, WorkflowStatus, WorkflowTransition
from app.schemas.business import ReplyTemplateCreateRequest, ReplyTemplateUpdateRequest, SapSnSyncApprovalRequest, SystemConfigUpdateRequest
from app.services.ai import multimodal_ai_configured, text_ai_configured
from app.services.common import model_to_dict
from app.services.external_relay import relay_configuration_status
from app.services.mail_test_preflight import MailTestPreflightError, run_mail_test_preflight
from app.services.mail_safety import test_mail_configuration_reasons
from app.services.rma_test_preflight import build_rma_test_preflight
from app.services.runtime_config import read_runtime_config, write_runtime_config
from app.services.sap_sn_sync import apply_sn_sync_batch, create_sn_sync_batch, serialize_sync_batch
from app.services.storage import find_orphan_oss_objects

router = APIRouter()


@router.post("/sap-sn-sync")
async def start_sap_sn_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    result = await create_sn_sync_batch(session, user_id=current_user.id)
    await session.commit()
    return ok(result, "SAP SN full snapshot completed")


@router.get("/sap-sn-sync/{batch_id}")
async def get_sap_sn_sync(
    batch_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    batch = await session.get(SapSnSyncBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SAP_SN_SYNC_BATCH_NOT_FOUND")
    return ok(serialize_sync_batch(batch))


@router.post("/sap-sn-sync/{batch_id}/apply")
async def approve_sap_sn_sync(
    batch_id: int,
    payload: SapSnSyncApprovalRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    try:
        result = await apply_sn_sync_batch(
            session,
            batch_id=batch_id,
            user_id=current_user.id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return ok(result, "SAP SN snapshot applied")


def _config_payload() -> dict:
    runtime = read_runtime_config()
    mail_test_reasons = test_mail_configuration_reasons()
    return {
        "auto_send_enabled": runtime["auto_send_enabled"],
        "auto_followup_enabled": runtime["auto_followup_enabled"],
        "rma_auto_send_enabled": runtime["rma_auto_send_enabled"],
        "reply_send_mode": "auto_send" if runtime["auto_send_enabled"] else "human_review",
        "auto_apply_min_confidence": runtime["auto_apply_min_confidence"],
        "auto_send_min_confidence": runtime["auto_send_min_confidence"],
        "max_follow_up": runtime["max_follow_up"],
        "confidence_threshold": runtime["confidence_threshold"],
        "environment_note": "仅允许测试邮箱发送；普通回复是主控，自动追问独立，RMA 开关只控制授权单附件。",
        "mail_test_static_ready": not mail_test_reasons,
        "mail_test_static_reasons": mail_test_reasons,
        "integrations": {
            "imap_configured": bool(settings.IMAP_HOST and settings.IMAP_USER and settings.IMAP_PASSWORD),
            "smtp_configured": bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD),
            "oss_configured": bool(settings.OSS_ENDPOINT and settings.OSS_BUCKET and settings.OSS_ACCESS_KEY and settings.OSS_SECRET_KEY),
            "ai_configured": text_ai_configured(),
            "text_ai_configured": text_ai_configured(),
            "text_ai_provider": "deepseek",
            "ai_model": settings.AI_MODEL,
            "ai_base_url": settings.AI_BASE_URL,
            "ai_prompt_version": settings.AI_PROMPT_VERSION,
            "ai_timeout_seconds": settings.AI_TIMEOUT_SECONDS,
            "multimodal_ai_configured": multimodal_ai_configured(),
            "multimodal_provider": settings.MULTIMODAL_PROVIDER,
            "qwen_vl_model": settings.QWEN_VL_MODEL or settings.QWEN_MODEL,
            "qwen_text_model": settings.QWEN_MODEL,
            "email_async_enabled": settings.EMAIL_ASYNC_ENABLED,
            "smtp_async_enabled": settings.SMTP_ASYNC_ENABLED,
            "import_export_async_enabled": settings.IMPORT_EXPORT_ASYNC_ENABLED,
            "relay_sn_sync_enabled": settings.RELAY_SN_SYNC_ENABLED,
            "relay_push_enabled": settings.RELAY_PUSH_ENABLED,
            "relay_configured": bool(settings.RELAY_BASE_URL and settings.RELAY_API_KEY),
            "sqlserver_relay": relay_configuration_status(),
        },
    }


@router.get("/info")
async def system_info(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    statuses = (await session.execute(select(WorkflowStatus).order_by(WorkflowStatus.sort_order, WorkflowStatus.id))).scalars().all()
    transitions = (await session.execute(select(WorkflowTransition).where(WorkflowTransition.enabled == True))).scalars().all()  # noqa: E712
    return ok(
        {
            "app": settings.APP_NAME,
            "env": settings.APP_ENV,
            **_config_payload(),
            "workflow_statuses": [
                model_to_dict(
                    status,
                    ("id", "status_code", "status_name", "status_category", "description", "is_terminal", "sort_order", "enabled"),
                )
                for status in statuses
            ],
            "workflow_transitions": [
                model_to_dict(
                    transition,
                    (
                        "id",
                        "from_status_code",
                        "to_status_code",
                        "trigger_event",
                        "condition_desc",
                        "require_manual",
                        "enabled",
                    ),
                )
                for transition in transitions
            ],
        }
    )


@router.get("/runtime-status")
async def runtime_status(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    latest_imap = await session.scalar(
        select(JobRunLog).where(JobRunLog.job_type == "imap_fetch").order_by(JobRunLog.created_at.desc()).limit(1)
    )
    failed_jobs = int(
        await session.scalar(select(func.count()).select_from(JobRunLog).where(JobRunLog.status.in_(["failed", "needs_manual_review"]))) or 0
    )
    retry_jobs = int(
        await session.scalar(select(func.count()).select_from(JobRunLog).where(JobRunLog.status == "retry_wait")) or 0
    )
    imap_retry_count = int(
        await session.scalar(select(func.count()).select_from(MailFetchRecord).where(MailFetchRecord.fetch_status == "retry_wait")) or 0
    )
    orphan_objects = await find_orphan_oss_objects(session, limit=1000)
    provider_status: dict[str, dict | None] = {}
    for provider in ("deepseek", "qwen"):
        latest = await session.scalar(
            select(AiCallLog).where(AiCallLog.provider_name == provider).order_by(AiCallLog.created_at.desc()).limit(1)
        )
        provider_status[provider] = (
            {
                "status": latest.status,
                "model": latest.model_name,
                "error_code": latest.error_code,
                "latency_ms": latest.latency_ms,
                "created_at": latest.created_at,
            }
            if latest else None
        )
    return ok({
        "latest_imap_job": model_to_dict(latest_imap, ("id", "status", "processed_count", "success_count", "failed_count", "error_code", "created_at", "finished_at")) if latest_imap else None,
        "failed_job_count": failed_jobs,
        "retry_job_count": retry_jobs,
        "imap_retry_count": imap_retry_count,
        "oss_orphan_count": len(orphan_objects),
        "oss_orphans_truncated": len(orphan_objects) >= 1000,
        "ai_provider_status": provider_status,
    })


@router.get("/config")
async def get_config(current_user: Annotated[CurrentUser, Depends(require_roles("admin"))]) -> dict:
    del current_user
    return ok(_config_payload())


@router.get("/integrations/sqlserver/status")
async def sqlserver_status(
    current_user: Annotated[CurrentUser, Depends(require_roles("operator"))],
) -> dict:
    del current_user
    return ok(relay_configuration_status())


@router.patch("/config")
async def update_config(
    payload: SystemConfigUpdateRequest,
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    values = payload.model_dump(exclude_unset=True)
    current = read_runtime_config()
    enabling_send = any(
        values.get(key) is True and not bool(current.get(key))
        for key in ("auto_send_enabled", "auto_followup_enabled", "rma_auto_send_enabled")
    ) or (values.get("reply_send_mode") == "auto_send" and not current["auto_send_enabled"])
    if enabling_send:
        try:
            await run_mail_test_preflight()
        except MailTestPreflightError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "MAIL_TEST_PREFLIGHT_REQUIRED", "data": {"preflight": exc.result}},
            ) from exc
    write_runtime_config(values)
    return ok(_config_payload(), "system config updated")


@router.post("/mail-test/preflight")
async def mail_test_preflight(
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    try:
        result = await run_mail_test_preflight()
    except MailTestPreflightError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "MAIL_TEST_PREFLIGHT_FAILED", "data": {"preflight": exc.result}},
        ) from exc
    return ok(result, "mail test preflight passed without sending messages")


@router.post("/rma-test/preflight")
async def rma_test_preflight(
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    # This endpoint only builds and reparses local bytes. It never opens SMTP.
    return ok(build_rma_test_preflight().result, "RMA test email offline preflight completed")


def _reply_template_payload(template: ReplyTemplate) -> dict:
    return model_to_dict(
        template,
        (
            "id",
            "template_code",
            "template_name",
            "template_type",
            "language",
            "version",
            "subject_template",
            "body_template",
            "html_body_template",
            "enabled",
            "created_by_user_id",
            "created_at",
            "updated_at",
        ),
    )


@router.get("/reply-templates")
async def list_reply_templates(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    rows = (
        await session.execute(select(ReplyTemplate).order_by(ReplyTemplate.template_type, ReplyTemplate.template_code, ReplyTemplate.version))
    ).scalars().all()
    return ok([_reply_template_payload(row) for row in rows])


@router.post("/reply-templates")
async def create_reply_template(
    payload: ReplyTemplateCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    values = payload.model_dump()
    exists = await session.scalar(
        select(ReplyTemplate).where(ReplyTemplate.template_code == values["template_code"], ReplyTemplate.version == values["version"])
    )
    if exists is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="REPLY_TEMPLATE_ALREADY_EXISTS")
    template = ReplyTemplate(**values, created_by_user_id=current_user.id)
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return ok(_reply_template_payload(template), "reply template created")


@router.patch("/reply-templates/{template_id}")
async def update_reply_template(
    template_id: int,
    payload: ReplyTemplateUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    template = await session.get(ReplyTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="REPLY_TEMPLATE_NOT_FOUND")
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        setattr(template, key, value)
    template.created_by_user_id = template.created_by_user_id or current_user.id
    await session.commit()
    await session.refresh(template)
    return ok(_reply_template_payload(template), "reply template updated")


@router.delete("/reply-templates/{template_id}")
async def delete_reply_template(
    template_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    template = await session.get(ReplyTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="REPLY_TEMPLATE_NOT_FOUND")
    references = int(await session.scalar(select(func.count()).select_from(ReplyRecord).where(ReplyRecord.template_id == template.id)) or 0)
    if references:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="REPLY_TEMPLATE_IN_USE")
    payload = _reply_template_payload(template)
    await session.delete(template)
    await session.commit()
    return ok({"deleted": True, "template": payload}, "reply template deleted")
