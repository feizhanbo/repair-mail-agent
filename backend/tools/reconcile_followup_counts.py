from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.models import RepairTicket, ReplyRecord


async def reconcile(*, apply_changes: bool) -> int:
    changed = 0
    async with AsyncSessionLocal() as session:
        tickets = (await session.execute(select(RepairTicket).order_by(RepairTicket.id))).scalars().all()
        for ticket in tickets:
            actual = int(
                await session.scalar(
                    select(func.count(ReplyRecord.id)).where(
                        ReplyRecord.ticket_id == ticket.id,
                        ReplyRecord.reply_type == "missing_fields",
                        ReplyRecord.send_status == "sent",
                    )
                )
                or 0
            )
            if ticket.followup_count == actual:
                continue
            changed += 1
            print(f"ticket_id={ticket.id} ticket_no={ticket.ticket_no} stored={ticket.followup_count} actual={actual}")
            if apply_changes:
                ticket.followup_count = actual
                if ticket.max_followup_count < actual:
                    ticket.max_followup_count = actual
        if apply_changes:
            await session.commit()
        else:
            await session.rollback()
    print(f"mode={'apply' if apply_changes else 'dry-run'} changed={changed}")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute followup_count from successfully sent missing-field replies.")
    parser.add_argument("--apply", action="store_true", help="Persist changes. The default is a read-only dry run.")
    args = parser.parse_args()
    asyncio.run(reconcile(apply_changes=args.apply))


if __name__ == "__main__":
    main()
