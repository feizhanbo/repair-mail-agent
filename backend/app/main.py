from __future__ import annotations

import ipaddress
import logging
import os
import platform
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from app.api.v1.router import api_router
from app.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.errors import http_exception_handler, unhandled_exception_handler, validation_exception_handler
from app.core.request_context import (
    bind_request_context,
    normalize_correlation_id,
    normalize_request_id,
    reset_request_context,
)
from app.core.runtime_logging import configure_runtime_logging, runtime_log_directory_ready
from app.integrations.llm_gateway import public_llm_routes
from app.models import JobRunLog, WorkerLease
from app.services.common import utcnow
from app.services.rma_pdf import validate_rma_runtime_health
from app.services.runtime_config import load_runtime_config, read_runtime_config

logger = logging.getLogger(__name__)

def setup_logging():
    configure_runtime_logging()


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
        # continue to report the database error; environment settings have
        # already passed fail-closed startup validation.
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
    logger.info("HTTP API ready; background execution is disabled in this process")
    logger.info("SMTP whitelist configured: count=%d", len(settings.SMTP_RECIPIENT_WHITELIST))
    if not settings.SMTP_RECIPIENT_WHITELIST:
        logger.warning("SECURITY: SMTP_RECIPIENT_WHITELIST is empty, all outbound email will be blocked!")
    logger.info("Application ready", extra={"event": "application_ready"})
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("Application shutdown", extra={"event": "application_shutdown"})


app = FastAPI(
    title="邮件报修自动化系统",
    description="Repair mail automation backend",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.API_DOCS_ENABLED or settings.APP_ENV != "production" else None,
    redoc_url="/redoc" if settings.API_DOCS_ENABLED or settings.APP_ENV != "production" else None,
    openapi_url="/openapi.json" if settings.API_DOCS_ENABLED or settings.APP_ENV != "production" else None,
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
        "worker_lease": False,
        "worker_job_state": False,
        "runtime_config": bool(getattr(request.app.state, "runtime_config_loaded", False)),
        "runtime_log_directory": runtime_log_directory_ready(),
    }
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            now = utcnow()
            lease = await session.scalar(
                select(WorkerLease).where(
                    WorkerLease.environment == settings.AIRMA_WORKER_ENVIRONMENT,
                    WorkerLease.queue_name == settings.AIRMA_WORKER_QUEUE,
                    WorkerLease.lease_expires_at > now,
                )
            )
            checks["worker_lease"] = lease is not None
            if lease is not None:
                invalid_running_job = await session.scalar(
                    select(JobRunLog.id)
                    .where(
                        JobRunLog.status == "running",
                        or_(
                            JobRunLog.locked_by.is_(None),
                            JobRunLog.locked_by != lease.instance_id,
                            JobRunLog.fencing_token.is_(None),
                            JobRunLog.fencing_token != lease.fencing_token,
                            JobRunLog.execution_deadline_at.is_(None),
                            JobRunLog.execution_deadline_at <= now,
                        ),
                    )
                    .limit(1)
                )
                checks["worker_job_state"] = invalid_running_job is None
        checks["mysql"] = True
    except Exception:
        logger.exception("Readiness MySQL check failed", extra={"event": "readiness_check_failed", "component": "mysql"})
    ready = all(checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )

