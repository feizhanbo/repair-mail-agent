from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RepairTicket, TicketRelayExport
from app.services.common import utcnow
from app.services.external_relay import push_ticket_snapshot_to_relay


async def execute_ticket_relay_export(session: AsyncSession, *, export_id: int) -> dict:
    export = await session.get(TicketRelayExport, export_id, with_for_update=True)
    if export is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RELAY_EXPORT_NOT_FOUND")
    ticket = await session.get(RepairTicket, export.ticket_id, with_for_update=True)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")
    if ticket.version != export.ticket_version or ticket.safety_check_hash != export.payload_hash:
        export.status = "superseded"
        export.error_code = "TICKET_SNAPSHOT_SUPERSEDED"
        return {"status": "superseded", "export_id": export.id}
    export.status = "running"
    export.attempt_count += 1
    ticket.relay_export_status = "running"
    result = await push_ticket_snapshot_to_relay(export.payload_snapshot)
    if result.get("status") != "succeeded":
        export.status = "failed"
        export.error_code = f"RELAY_{str(result.get('status') or 'FAILED').upper()}"
        ticket.relay_export_status = "failed"
        return {"status": "failed", "error_code": export.error_code, "export_id": export.id}
    export.status = "succeeded"
    export.remote_record_key = result.get("remote_record_key")
    export.error_code = None
    export.error_message = None
    export.exported_at = utcnow()
    ticket.relay_export_status = "succeeded"
    return {"status": "succeeded", "export_id": export.id, "remote_record_key": export.remote_record_key}
