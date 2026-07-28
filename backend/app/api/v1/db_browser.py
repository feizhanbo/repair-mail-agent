from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.core.database import get_session
from app.core.response import ok

router = APIRouter()

_VALID_TABLE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

_ALLOWED_TABLES = {
    "ai_call_logs",
    "board_cards",
    "email_attachments",
    "email_threads",
    "email_ticket_links",
    "emails",
    "export_sap",
    "field_audit_logs",
    "job_run_logs",
    "manual_review_tasks",
    "notification_events",
    "operation_logs",
    "oss_objects",
    "parse_results",
    "repair_ticket_items",
    "repair_tickets",
    "reply_records",
    "reply_templates",
    "roles",
    "sn_assets",
    "sn_validation_results",
    "system_event_logs",
    "ticket_status_logs",
    "user_roles",
    "users",
    "workflow_statuses",
    "workflow_transitions",
}


@router.get("/tables")
async def list_tables(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
) -> dict:
    del current_user
    sql = text(
        """
        SELECT
            t.TABLE_NAME AS table_name,
            t.TABLE_COMMENT AS table_comment,
            c.COLUMN_NAME AS column_name,
            c.COLUMN_TYPE AS column_type,
            c.IS_NULLABLE AS is_nullable,
            c.COLUMN_DEFAULT AS column_default,
            c.COLUMN_COMMENT AS column_comment,
            c.ORDINAL_POSITION AS ordinal
        FROM information_schema.TABLES t
        JOIN information_schema.COLUMNS c ON t.TABLE_NAME = c.TABLE_NAME
            AND t.TABLE_SCHEMA = c.TABLE_SCHEMA
        WHERE t.TABLE_SCHEMA = DATABASE()
          AND t.TABLE_NAME != 'alembic_version'
        ORDER BY t.TABLE_NAME, c.ORDINAL_POSITION
        """
    )
    result = await session.execute(sql)

    tables: dict[str, dict] = {}
    for row in result.fetchall():
        table_name = row.table_name
        if table_name not in _ALLOWED_TABLES:
            continue
        if table_name not in tables:
            tables[table_name] = {
                "table_name": table_name,
                "table_comment": row.table_comment or "",
                "columns": [],
            }
        tables[table_name]["columns"].append(
            {
                "name": row.column_name,
                "type": row.column_type,
                "nullable": row.is_nullable == "YES",
                "default": row.column_default,
                "comment": row.column_comment or "",
            }
        )

    table_list = sorted(tables.values(), key=lambda table: table["table_name"])
    return ok({"tables": table_list, "total": len(table_list)})


@router.get("/tables/{table_name}/rows")
async def list_table_rows(
    table_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[CurrentUser, Depends(require_roles("admin"))],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=1000),
) -> dict:
    del current_user
    if not _VALID_TABLE.match(table_name) or table_name not in _ALLOWED_TABLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="DB_BROWSER_INVALID_TABLE")

    total = (await session.execute(text(f"SELECT COUNT(*) FROM `{table_name}`"))).scalar_one()
    offset = (page - 1) * page_size
    result = await session.execute(text(f"SELECT * FROM `{table_name}` LIMIT {page_size} OFFSET {offset}"))
    columns = list(result.keys())
    rows = [dict(row._mapping) for row in result.fetchall()]

    return ok({"rows": rows, "total": total, "page": page, "page_size": page_size, "columns": columns})
