from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings


EXPECTED_ALEMBIC_HEAD = "k8f3a4b5c6d7"
EXPECTED_BUSINESS_TABLE_COUNT = 35
REQUIRED_INDEXES = {
    "uk_emails_message_id",
    "uk_emails_source_content_sha256",
    "uk_mail_fetch_records",
    "idx_mail_fetch_records_retry",
    "uk_job_run_logs_idempotency",
    "idx_job_run_logs_queue",
    "idx_repair_tickets_sn_validation_status",
    "uk_ticket_relay_export_snapshot",
    "uk_notification_user_state",
    "idx_emails_intent_subtype",
    "idx_email_threads_predecessor",
    "idx_reply_records_archive_retry",
    "uk_external_operation_type_key",
    "idx_external_operation_status_retry",
    "idx_notifications_ticket_attention",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_remote_mysql_schema_via_explicit_ssh_tunnel() -> None:
    database_url = settings.DB_SMOKE_DATABASE_URL.strip()
    if not database_url:
        pytest.skip("DB_SMOKE_DATABASE_URL is not configured")

    parsed = make_url(database_url)
    assert parsed.host in {"127.0.0.1", "localhost"}, "DB smoke test must use a local SSH tunnel"
    assert parsed.drivername.startswith("mysql+"), "DB smoke test requires an async MySQL URL"
    if parsed.drivername == "mysql+aiomysql":
        parsed = parsed.set(drivername="mysql+asyncmy")
        database_url = parsed.render_as_string(hide_password=False)

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
            head = await connection.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
            assert head == EXPECTED_ALEMBIC_HEAD

            table_count = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
                    "AND table_name <> 'alembic_version'"
                )
            )
            assert int(table_count or 0) == EXPECTED_BUSINESS_TABLE_COUNT

            view_count = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.views "
                    "WHERE table_schema = DATABASE() AND table_name = 'business_emails'"
                )
            )
            assert int(view_count or 0) == 1

            rows = await connection.execute(
                text(
                    "SELECT DISTINCT index_name FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE()"
                )
            )
            index_names = {str(row[0]) for row in rows}
            assert REQUIRED_INDEXES <= index_names

            foreign_key_count = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.referential_constraints "
                    "WHERE constraint_schema = DATABASE()"
                )
            )
            assert int(foreign_key_count or 0) > 0
    finally:
        await engine.dispose()
