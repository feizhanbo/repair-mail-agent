from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from app.api.v1.router import api_router
from app.config import settings
from app.core.database import AsyncSessionLocal
from app.core.errors import http_exception_handler, unhandled_exception_handler, validation_exception_handler
from app.core.request_context import bind_request_context, normalize_correlation_id, reset_request_context
from app.models import JobRunLog
from app.services.ai import maintain_ai_jsonl_logs
from app.services.jobs import (
    JobLeaseLost,
    claim_next_job,
    enqueue_job,
    execute_claimed_job,
    recover_stale_jobs,
    renew_job_lease,
)
from app.services.notification_task_repair import repair_notification_and_task_data
from app.services.rma_pdf import validate_rma_runtime_health
from app.services.release_evidence import verify_runtime_release_gate
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
            owner_token = str(job.locked_by or "")
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
                await _execute_job_with_lease(
                    run_session,
                    claimed_job,
                    owner_token=owner_token,
                )
            finally:
                reset_request_context(tokens)


async def _execute_job_with_lease(
    run_session,
    claimed_job: JobRunLog,
    *,
    owner_token: str,
) -> bool:
    """Run one claimed job while a separately committed lease heartbeat fences it."""
    stop_heartbeat = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _job_lease_heartbeat(
            job_id=claimed_job.id,
            owner_token=owner_token,
            stop=stop_heartbeat,
            lease_lost=lease_lost,
        )
    )
    execution = asyncio.create_task(
        execute_claimed_job(
            run_session,
            claimed_job,
            expected_owner_token=owner_token,
        )
    )
    try:
        done, _pending = await asyncio.wait(
            {execution, heartbeat},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat in done and lease_lost.is_set() and not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            await run_session.rollback()
            return False
        await execution
        if lease_lost.is_set():
            await run_session.rollback()
            return False
        await run_session.commit()
        return True
    except JobLeaseLost:
        await run_session.rollback()
        return False
    finally:
        stop_heartbeat.set()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def _job_lease_heartbeat(
    *,
    job_id: int,
    owner_token: str,
    stop: asyncio.Event,
    lease_lost: asyncio.Event,
) -> None:
    interval = _job_lease_heartbeat_interval(settings.ASYNC_JOB_STALE_SECONDS)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            async with AsyncSessionLocal() as session:
                renewed = await renew_job_lease(
                    session,
                    job_id=job_id,
                    owner_token=owner_token,
                )
                await session.commit()
        except Exception:
            logger.exception("Job lease heartbeat failed: job_id=%s", job_id)
            lease_lost.set()
            return
        if not renewed:
            lease_lost.set()
            return


def _job_lease_heartbeat_interval(stale_seconds: int) -> int:
    """Keep at least three renewal opportunities inside the stale window."""
    return max(1, min(30, stale_seconds // 3))


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
    release_gate = verify_runtime_release_gate(settings)
    app.state.langgraph_release_gate = (
        {
            "required": True,
            "verified": True,
            **release_gate,
        }
        if release_gate is not None
        else {
            "required": False,
            "verified": False,
            "workflow_engine": settings.WORKFLOW_ENGINE,
        }
    )
    if release_gate is not None:
        logger.info(
            "LangGraph release evidence verified: schema=%s source_commit=%s sha256=%s",
            release_gate["schema_version"],
            release_gate["source_commit"],
            release_gate["sha256"],
        )
    logger.info(
        "Application starting: app_name=%s app_env=%s text_ai_provider=deepseek multimodal_provider=%s",
        settings.APP_NAME,
        settings.APP_ENV,
        settings.MULTIMODAL_PROVIDER,
    )
    read_runtime_config()
    rma_health = validate_rma_runtime_health()
    logger.info(
        "RMA PDF runtime healthy: template_version=%s template_sha256=%s cjk_font=%s",
        rma_health["template_version"],
        rma_health["template_sha256"],
        rma_health["cjk_font"],
    )
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

