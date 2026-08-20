from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.exc import SQLAlchemyError
from app.api.v1.router import api_router
from app.config import settings
from app.core.database import AsyncSessionLocal
from app.core.errors import http_exception_handler, unhandled_exception_handler, validation_exception_handler
from app.core.request_context import bind_request_context, normalize_correlation_id, reset_request_context
from app.models import JobRunLog
from app.services.ai import maintain_ai_jsonl_logs
from app.services.jobs import claim_next_job, enqueue_job, execute_claimed_job, recover_stale_jobs
from app.services.notification_task_repair import repair_notification_and_task_data
from app.services.rma_pdf import validate_rma_runtime_health
from app.services.runtime_config import load_runtime_config, read_runtime_config
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
    try:
        async with AsyncSessionLocal() as session:
            await load_runtime_config(session)
            if not settings.IMAP_FETCH_ENABLED:
                logger.info("Scheduled IMAP fetch skipped: IMAP_FETCH_ENABLED=false")
                return
            if not settings.IMAP_ARCHIVE_TO_OSS:
                logger.error("Scheduled IMAP fetch skipped: IMAP_ARCHIVE_TO_OSS must be true")
                return
            interval = max(1, settings.IMAP_POLL_INTERVAL_MINUTES)
            now = utcnow()
            bucket = now.replace(minute=(now.minute // interval) * interval, second=0, microsecond=0)
            job = await enqueue_job(
                session,
                job_type="imap_fetch",
                resource_type="mailbox",
                resource_id=None,
                idempotency_key=f"imap_fetch:scheduled:{settings.IMAP_USER}:{settings.IMAP_FOLDER}:{bucket.isoformat()}",
                metadata={
                    "folder_name": settings.IMAP_FOLDER,
                    "limit": settings.IMAP_FETCH_LIMIT,
                    "unseen_only": settings.IMAP_UNSEEN_ONLY,
                    "auto_parse": True,
                },
            )
            await session.commit()
            logger.info("Scheduled IMAP fetch queued: job_id=%s", job.id)
    except Exception:
        logger.exception("Scheduled IMAP fetch failed")


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


async def _scheduled_consistency_recovery():
    try:
        async with AsyncSessionLocal() as session:
            stale_jobs = await recover_stale_jobs(session)
            repair_result = await repair_notification_and_task_data(session, apply=True)
            await session.commit()
        logger.info(
            "Consistency recovery completed: stale_jobs=%d normalized_tasks=%d",
            stale_jobs,
            repair_result.get("counts", {}).get("normalized_tasks", 0),
        )
    except Exception:
        logger.exception("Consistency recovery deferred because the database is unavailable")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info(
        "Application starting: app_name=%s app_env=%s text_ai_provider=deepseek multimodal_provider=%s",
        settings.APP_NAME,
        settings.APP_ENV,
        settings.MULTIMODAL_PROVIDER,
    )
    try:
        async with AsyncSessionLocal() as config_session:
            await load_runtime_config(config_session)
            await config_session.commit()
    except SQLAlchemyError:
        # Health/error routes and isolated API contract tests must still start
        # when the database is temporarily unavailable. Business endpoints will
        # continue to report the database error; settings use safe env defaults.
        logger.exception("Database-backed runtime config unavailable at startup; using safe defaults")
        read_runtime_config()
    rma_health = validate_rma_runtime_health()
    logger.info(
        "RMA PDF runtime healthy: template_version=%s template_sha256=%s cjk_font=%s",
        rma_health["template_version"],
        rma_health["template_sha256"],
        rma_health["cjk_font"],
    )
    scheduler = AsyncIOScheduler()
    # Run the lightweight gate every minute so database-backed enable/interval
    # changes become effective without restarting the process. The idempotency
    # bucket inside _scheduled_imap_fetch enforces the configured interval.
    scheduler.add_job(
        _scheduled_imap_fetch,
        "interval",
        minutes=1,
        id="imap_poll",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
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
    scheduler.add_job(
        _scheduled_consistency_recovery,
        "interval",
        minutes=5,
        next_run_time=utcnow() + timedelta(seconds=5),
        id="consistency_recovery",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Schedulers started: IMAP poll interval=%d min; follow-ups are event-driven", settings.IMAP_POLL_INTERVAL_MINUTES)
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
    docs_url="/docs" if settings.API_DOCS_ENABLED or settings.APP_ENV.lower() not in {"prod", "production"} else None,
    redoc_url="/redoc" if settings.API_DOCS_ENABLED or settings.APP_ENV.lower() not in {"prod", "production"} else None,
    openapi_url="/openapi.json" if settings.API_DOCS_ENABLED or settings.APP_ENV.lower() not in {"prod", "production"} else None,
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.TRUSTED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
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
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
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

