from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text

from app.api.v1.router import api_router
from app.config import settings
from app.core.database import AsyncSessionLocal
from app.core.errors import http_exception_handler, unhandled_exception_handler, validation_exception_handler
from app.core.request_context import bind_request_context, normalize_correlation_id, reset_request_context
from app.models import JobRunLog, RepairTicket
from app.services.imap_fetcher import fetch_imap_emails
from app.services.ai import maintain_ai_jsonl_logs
from app.services.jobs import claim_next_job, execute_claimed_job
from app.services.replies import create_reply_draft
from app.services.runtime_config import read_runtime_config
from app.services.common import utcnow

logger = logging.getLogger(__name__)

STRUCTURED_FORMAT = '{"time": "%(asctime)s", "level": "%(levelname)s", "module": "%(name)s", "message": "%(message)s"}'


def setup_logging():
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(STRUCTURED_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S.%%f"))
    root_logger.addHandler(handler)


async def _scheduled_imap_fetch():
    if not settings.IMAP_FETCH_ENABLED:
        logger.info("Scheduled IMAP fetch skipped: IMAP_FETCH_ENABLED=false")
        return
    try:
        async with AsyncSessionLocal() as session:
            lock_name = "repair_mail_agent_imap_poll"
            acquired = await session.scalar(text("SELECT GET_LOCK(:lock_name, 0)"), {"lock_name": lock_name})
            if acquired != 1:
                logger.info("Scheduled IMAP fetch skipped: distributed lock unavailable")
                return
            try:
                result = await fetch_imap_emails(
                    session,
                    folder_name=settings.IMAP_FOLDER,
                    limit=settings.IMAP_FETCH_LIMIT,
                    unseen_only=settings.IMAP_UNSEEN_ONLY,
                    archive_to_oss=settings.IMAP_ARCHIVE_TO_OSS,
                )
                await session.commit()
            finally:
                await session.scalar(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name})
            logger.info(
                "Scheduled IMAP fetch completed: job_id=%s status=%s processed=%s success=%s failed=%s",
                result.get("job_id"),
                result.get("status"),
                result.get("processed_count"),
                result.get("success_count"),
                result.get("failed_count"),
            )
    except Exception:
        logger.exception("Scheduled IMAP fetch failed")


async def _scheduled_auto_followup():
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(RepairTicket).where(RepairTicket.current_status_code == "need_customer_info")
            )
            tickets = result.scalars().all()
            processed = 0
            skipped = 0
            errors = 0
            for ticket in tickets:
                if ticket.followup_count >= ticket.max_followup_count:
                    skipped += 1
                    continue
                if not settings.AUTO_SEND_ENABLED:
                    skipped += 1
                    continue
                try:
                    await create_reply_draft(session, ticket_id=ticket.id, user_id=None)
                    processed += 1
                except Exception:
                    logger.exception("Auto follow-up failed for ticket %s", ticket.ticket_no)
                    errors += 1
            await session.commit()
            logger.info(
                "Scheduled auto follow-up completed: total=%d, processed=%d, skipped=%d, errors=%d",
                len(tickets),
                processed,
                skipped,
                errors,
            )
    except Exception:
        logger.exception("Scheduled auto follow-up failed")


async def _scheduled_job_worker():
    for _ in range(10):
        async with AsyncSessionLocal() as claim_session:
            job = await claim_next_job(claim_session)
            if job is None:
                await claim_session.commit()
                return
            job_id = job.id
            await claim_session.commit()
        async with AsyncSessionLocal() as run_session:
            claimed_job = await run_session.get(JobRunLog, job_id)
            if claimed_job is None or claimed_job.status != "running":
                continue
            from app.core.request_context import bind_request_context, reset_request_context

            tokens = bind_request_context(
                correlation_id=claimed_job.correlation_id or f"job-{claimed_job.id}",
                client_ip=None,
                user_agent="background-job-worker",
            )
            try:
                await execute_claimed_job(run_session, claimed_job)
                await run_session.commit()
            finally:
                reset_request_context(tokens)


async def _scheduled_ai_log_maintenance():
    async with AsyncSessionLocal() as session:
        result = await maintain_ai_jsonl_logs(session)
        await session.commit()
    logger.info(
        "AI JSONL maintenance completed: sanitized_files=%d deleted_files=%d",
        result["sanitized_files"],
        result["deleted_files"],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info(
        "Application starting: app_name=%s app_env=%s text_ai_provider=deepseek multimodal_provider=%s",
        settings.APP_NAME,
        settings.APP_ENV,
        settings.MULTIMODAL_PROVIDER,
    )
    read_runtime_config()
    scheduler = AsyncIOScheduler()
    if settings.IMAP_FETCH_ENABLED and settings.IMAP_ARCHIVE_TO_OSS:
        scheduler.add_job(
            _scheduled_imap_fetch,
            "interval",
            minutes=settings.IMAP_POLL_INTERVAL_MINUTES,
            id="imap_poll",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    elif settings.IMAP_FETCH_ENABLED:
        logger.error("IMAP scheduler disabled: IMAP_ARCHIVE_TO_OSS must be true")
    scheduler.add_job(
        _scheduled_auto_followup,
        "interval",
        minutes=settings.AUTO_FOLLOWUP_INTERVAL_MINUTES,
        id="auto_followup",
        replace_existing=True,
    )
    scheduler.add_job(
        _scheduled_job_worker,
        "interval",
        seconds=max(1, settings.ASYNC_JOB_POLL_SECONDS),
        id="background_job_worker",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _scheduled_ai_log_maintenance,
        "interval",
        hours=24,
        next_run_time=utcnow(),
        id="ai_log_maintenance",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Schedulers started: IMAP poll interval=%d min, auto follow-up interval=%d min", settings.IMAP_POLL_INTERVAL_MINUTES, settings.AUTO_FOLLOWUP_INTERVAL_MINUTES)
    logger.info("SMTP whitelist configured: count=%d", len(settings.SMTP_RECIPIENT_WHITELIST))
    if not settings.SMTP_RECIPIENT_WHITELIST:
        logger.warning("SECURITY: SMTP_RECIPIENT_WHITELIST is empty, all outbound email will be blocked!")
    try:
        yield
    finally:
        scheduler.shutdown()
        logger.info("Schedulers shutdown")


app = FastAPI(
    title="邮件报修自动化系统",
    description="Repair mail automation backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = normalize_correlation_id(request.headers.get("X-Correlation-ID"))
    tokens = bind_request_context(
        correlation_id=correlation_id,
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    try:
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response
    finally:
        reset_request_context(tokens)

app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": settings.APP_NAME, "env": settings.APP_ENV}

