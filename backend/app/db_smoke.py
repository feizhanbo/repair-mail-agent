from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import text

from app.core.database import engine


async def run_db_smoke() -> dict[str, Any]:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TEMPORARY TABLE codex_db_smoke (id INT PRIMARY KEY, value VARCHAR(32))"))
        await conn.execute(text("INSERT INTO codex_db_smoke (id, value) VALUES (1, 'ok')"))

        smoke = (await conn.execute(text("SELECT value FROM codex_db_smoke WHERE id = 1"))).scalar_one()
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        table_count = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
                )
            )
        ).scalar_one()
        workflow_statuses = (await conn.execute(text("SELECT COUNT(*) FROM workflow_statuses"))).scalar_one()
        workflow_transitions = (await conn.execute(text("SELECT COUNT(*) FROM workflow_transitions"))).scalar_one()
        roles = (await conn.execute(text("SELECT COUNT(*) FROM roles"))).scalar_one()
        users = (await conn.execute(text("SELECT COUNT(*) FROM users"))).scalar_one()
        reply_templates = (await conn.execute(text("SELECT COUNT(*) FROM reply_templates"))).scalar_one()

        await conn.execute(text("DROP TEMPORARY TABLE codex_db_smoke"))

    return {
        "smoke": smoke,
        "alembic": version,
        "tables": table_count,
        "workflow_statuses": workflow_statuses,
        "workflow_transitions": workflow_transitions,
        "roles": roles,
        "users": users,
        "reply_templates": reply_templates,
    }


async def _main() -> None:
    try:
        result = await run_db_smoke()
        for key, value in result.items():
            print(f"{key}: {value}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
