from __future__ import annotations

from collections.abc import AsyncGenerator
import hashlib
import logging
import re
import time

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=False)
logger = logging.getLogger(__name__)
_SPACE = re.compile(r"\s+")


def _statement_summary(statement: object) -> tuple[str, str]:
    normalized = _SPACE.sub(" ", str(statement or "").strip())
    operation = normalized.split(" ", 1)[0].upper() if normalized else "UNKNOWN"
    return operation, hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@event.listens_for(engine.sync_engine, "before_cursor_execute")
def _before_cursor_execute(conn, _cursor, _statement, _parameters, _context, _executemany) -> None:
    conn.info.setdefault("query_started_at", []).append(time.perf_counter())


@event.listens_for(engine.sync_engine, "after_cursor_execute")
def _after_cursor_execute(conn, _cursor, statement, _parameters, _context, _executemany) -> None:
    starts = conn.info.get("query_started_at") or []
    if not starts:
        return
    duration_ms = int((time.perf_counter() - starts.pop()) * 1000)
    if duration_ms < settings.SLOW_DB_THRESHOLD_MS:
        return
    operation, fingerprint = _statement_summary(statement)
    logger.warning(
        "Slow database query",
        extra={
            "event": "slow_db_query",
            "db_system": engine.url.get_backend_name(),
            "db_operation": operation,
            "statement_fingerprint": fingerprint,
            "duration_ms": duration_ms,
        },
    )


@event.listens_for(engine.sync_engine, "handle_error")
def _handle_database_error(context) -> None:
    operation, fingerprint = _statement_summary(getattr(context, "statement", None))
    original = getattr(context, "original_exception", None)
    duration_ms: int | None = None
    connection = getattr(context, "connection", None)
    if connection is not None:
        starts = connection.info.get("query_started_at") or []
        if starts:
            duration_ms = int((time.perf_counter() - starts.pop()) * 1000)
    logger.error(
        "Database operation failed",
        extra={
            "event": "database_operation_failed",
            "db_system": engine.url.get_backend_name(),
            "db_operation": operation,
            "statement_fingerprint": fingerprint,
            "error_type": type(original).__name__ if original else "DatabaseError",
            "error_code": "DB_OPERATION_FAILED",
            "duration_ms": duration_ms,
        },
    )


if engine.url.get_backend_name() == "mysql":
    @event.listens_for(engine.sync_engine, "connect")
    def _set_mysql_utc(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET time_zone = '+00:00'")
        finally:
            cursor.close()


AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session

