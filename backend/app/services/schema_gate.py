from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.mail_test_preflight import REQUIRED_DATABASE_REVISION


def expected_schema_heads() -> set[str]:
    return {REQUIRED_DATABASE_REVISION}


async def assert_schema_current(session: AsyncSession) -> None:
    rows = await session.execute(text("SELECT version_num FROM alembic_version"))
    actual = {str(value) for value in rows.scalars().all()}
    expected = expected_schema_heads()
    if actual != expected:
        raise RuntimeError(
            "WORKER_SCHEMA_REVISION_MISMATCH:"
            f"expected={','.join(sorted(expected))}:actual={','.join(sorted(actual)) or 'missing'}"
        )
