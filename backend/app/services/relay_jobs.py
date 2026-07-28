from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.sap_rma import submit_export_batch


async def execute_ticket_relay_export(session: AsyncSession, *, export_id: int) -> dict:
    return await submit_export_batch(session, export_id=export_id)
