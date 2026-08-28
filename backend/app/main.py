from __future__ import annotations

import ipaddress
import logging
import os
import platform
import time
from contextlib import asynccontextmanager
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from app.api.v1.router import api_router
from app.config import settings
from app.core.database import AsyncSessionLocal
from app.core.errors import http_exception_handler, unhandled_exception_handler, validation_exception_handler
from app.core.request_context import (
    bind_request_context,
    normalize_correlation_id,
    normalize_request_id,
    reset_request_context,
)
from app.core.runtime_logging import configure_runtime_logging, runtime_log_directory_ready
from app.models import JobRunLog
from app.integrations.llm_gateway import public_llm_routes
from app.services.ai import maintain_ai_jsonl_logs
from app.services.jobs import claim_next_job, enqueue_job, execute_claimed_job, recover_stale_jobs
from app.services.notification_task_repair import repair_notification_and_task_data
from app.services.rma_pdf import validate_rma_runtime_health
from app.services.runtime_config import load_runtime_config, read_runtime_config
from app.services.common import utcnow

logger = logging.getLogger(__name__)

def setup_logging():
    configure_runtime_logging()


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
                request_id=None,
                correlation_id=claimed_job.correlation_id or f"job-{claimed_job.id}",
                client_ip=None,
                user_agent="background-job-worker",
                job_run_id=claimed_job.id,
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


async def _scheduled_sap_sn_sync():
    if not settings.RELAY_SN_SYNC_ENABLED or settings.RELAY_ADAPTER.strip().lower() != "sqlserver":
        return
    try:
        from app.services.sap_sn_sync import create_sn_sync_batch

        async with AsyncSessionLocal() as session:
            result = await create_sn_sync_batch(session)
            await session.commit()
        logger.info(
            "Scheduled SAP SN snapshot completed",
            extra={"event": "sap_sn_sync_scheduled", "status": result.get("status"), "source_count": result.get("source_count")},
        )
    except Exception:
        logger.exception("Scheduled SAP SN snapshot failed", extra={"event": "sap_sn_sync_scheduled_failed"})


async def _scheduled_sap_rma_poll():
    if not settings.RELAY_SQLSERVER_ENABLED:
        return
    try:
        from app.services.sap_rma import poll_waiting_rma_results

        async with AsyncSessionLocal() as session:
            result = await poll_waiting_rma_results(session)
            await session.commit()
        logger.info(
            "Scheduled SAP RMA2 polling completed",
            extra={
                "event": "sap_rma2_poll_scheduled",
                "status": result.get("status"),
                "request_count": result.get("request_count", 0),
                "result_count": result.get("result_count", 0),
            },
        )
    except Exception:
        logger.exception(
            "Scheduled SAP RMA2 polling failed",
            extra={"event": "sap_rma2_poll_scheduled_failed"},
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.runtime_config_loaded = False
    llm_routes = public_llm_routes()
    logger.info(
        "Application starting",
        extra={
            "event": "application_starting",
            "app_version": settings.APP_VERSION,
            "commit_sha": settings.COMMIT_SHA,
            "python_version": platform.python_version(),
            "worker_pid": os.getpid(),
            "llm_routes": {task: row["primary"] for task, row in llm_routes.items()},
        },
    )
    try:
        async with AsyncSessionLocal() as config_session:
            await load_runtime_config(config_session)
        await config_session.commit()
        app.state.runtime_config_loaded = True
        logger.info("Runtime configuration loaded", extra={"event": "configuration_loaded"})
        logger.info("MySQL connection ready", extra={"event": "mysql_connection_ready"})
    except SQLAlchemyError:
        # Health/error routes and isolated API contract tests must still start
        # when the database is temporarily unavailable. Business endpoints will
        # continue to report the database error; settings use safe env defaults.
        logger.critical(
            "Database-backed runtime config unavailable at startup; using safe defaults",
            exc_info=True,
            extra={"event": "mysql_connection_failed", "error_code": "MYSQL_STARTUP_UNAVAILABLE"},
        )
        read_runtime_config()
        app.state.runtime_config_loaded = True
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
    scheduler.add_job(
        _scheduled_sap_sn_sync,
        "cron",
        hour=max(0, min(23, settings.RELAY_SQLSERVER_FULL_SYNC_HOUR)),
        minute=0,
        timezone="Asia/Shanghai",
        id="sap_sn_daily_full_snapshot",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _scheduled_sap_rma_poll,
        "interval",
        seconds=max(1, settings.RELAY_SQLSERVER_RMA_POLL_INTERVAL_SECONDS),
        next_run_time=utcnow() + timedelta(seconds=10),
        id="sap_rma2_batch_poll",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        "Schedulers started: IMAP poll interval=%d min; follow-ups are event-driven",
        settings.IMAP_POLL_INTERVAL_MINUTES,
        extra={"event": "scheduler_initialized"},
    )
    logger.info("SMTP whitelist configured: count=%d", len(settings.SMTP_RECIPIENT_WHITELIST))
    if not settings.SMTP_RECIPIENT_WHITELIST:
        logger.warning("SECURITY: SMTP_RECIPIENT_WHITELIST is empty, all outbound email will be blocked!")
    logger.info("Application ready", extra={"event": "application_ready"})
    try:
        yield
    finally:
        scheduler.shutdown()
        logger.info("Application shutdown", extra={"event": "application_shutdown"})


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
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID", "X-Request-ID"],
    expose_headers=["X-Correlation-ID", "X-Request-ID"],
)


def _trusted_proxy(address: str | None) -> bool:
    if not address:
        return False
    try:
        candidate = ipaddress.ip_address(address)
        return any(candidate in ipaddress.ip_network(cidr, strict=False) for cidr in settings.TRUSTED_PROXY_CIDRS)
    except ValueError:
        return False


def _client_ip(request: Request) -> str | None:
    peer = request.client.host if request.client else None
    if not _trusted_proxy(peer):
        return peer
    real_ip = (request.headers.get("X-Real-IP") or "").strip()
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    candidate = real_ip or forwarded
    try:
        return str(ipaddress.ip_address(candidate)) if candidate else peer
    except ValueError:
        return peer


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    request_id = normalize_request_id(request.headers.get("X-Request-ID"))
    correlation_id = normalize_correlation_id(request.headers.get("X-Correlation-ID"))
    tokens = bind_request_context(
        request_id=request_id,
        correlation_id=correlation_id,
        client_ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    started = time.perf_counter()
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            # Route exceptions cross the function middleware before FastAPI's
            # outer exception handler can build a response. Handle them here so
            # the 500 response keeps the same request/correlation headers and
            # the traceback is emitted while ContextVars are still bound.
            response = await unhandled_exception_handler(request, exc)
        duration_ms = int((time.perf_counter() - started) * 1000)
        route = request.scope.get("route")
        route_path = getattr(route, "path", None) or request.url.path
        if settings.HTTP_ACCESS_LOG_ENABLED:
            extra = {
                "event": "http_request_completed" if response.status_code < 500 else "http_request_failed",
                "request_id": request_id,
                "correlation_id": correlation_id,
                "http_method": request.method,
                "http_path": request.url.path,
                "http_route": route_path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "response_size": response.headers.get("content-length"),
                "slow": duration_ms >= settings.SLOW_REQUEST_THRESHOLD_MS,
            }
            if request.url.path in {"/health", "/readiness"} and response.status_code < 400:
                logger.debug("Health request completed", extra=extra)
            elif response.status_code >= 500:
                logger.error("HTTP request failed", extra=extra)
            elif duration_ms >= settings.SLOW_REQUEST_THRESHOLD_MS:
                logger.warning("Slow HTTP request completed", extra=extra)
            else:
                logger.info("HTTP request completed", extra=extra)
        response.headers["X-Request-ID"] = request_id
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


@app.get("/readiness", tags=["health"])
async def readiness_check(request: Request) -> JSONResponse:
    checks = {
        "mysql": False,
        "runtime_config": bool(getattr(request.app.state, "runtime_config_loaded", False)),
        "runtime_log_directory": runtime_log_directory_ready(),
    }
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["mysql"] = True
    except Exception:
        logger.exception("Readiness MySQL check failed", extra={"event": "readiness_check_failed", "component": "mysql"})
    ready = all(checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )

