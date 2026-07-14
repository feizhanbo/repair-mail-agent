from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api.v1.router import api_router
from app.config import settings
from app.core.database import AsyncSessionLocal
from app.core.errors import http_exception_handler, unhandled_exception_handler, validation_exception_handler
from app.models import RepairTicket
from app.services.imap_fetcher import fetch_imap_emails
from app.services.replies import create_reply_draft
from app.services.runtime_config import read_runtime_config

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
            result = await fetch_imap_emails(
                session,
                folder_name=settings.IMAP_FOLDER,
                limit=settings.IMAP_FETCH_LIMIT,
                unseen_only=settings.IMAP_UNSEEN_ONLY,
                archive_to_oss=settings.IMAP_ARCHIVE_TO_OSS,
            )
            await session.commit()
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
    if settings.IMAP_FETCH_ENABLED:
        scheduler.add_job(
            _scheduled_imap_fetch,
            "interval",
            minutes=settings.IMAP_POLL_INTERVAL_MINUTES,
            id="imap_poll",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    scheduler.add_job(
        _scheduled_auto_followup,
        "interval",
        minutes=settings.AUTO_FOLLOWUP_INTERVAL_MINUTES,
        id="auto_followup",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Schedulers started: IMAP poll interval=%d min, auto follow-up interval=%d min", settings.IMAP_POLL_INTERVAL_MINUTES, settings.AUTO_FOLLOWUP_INTERVAL_MINUTES)
    logger.info("SMTP whitelist configured: %s", settings.SMTP_RECIPIENT_WHITELIST if settings.SMTP_RECIPIENT_WHITELIST else "EMPTY - ALL OUTBOUND EMAIL BLOCKED")
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

app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": settings.APP_NAME, "env": settings.APP_ENV}

