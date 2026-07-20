from __future__ import annotations

import argparse
import asyncio
import json

from app.core.database import AsyncSessionLocal
from app.services.notification_task_repair import repair_notification_and_task_data


async def _run(*, apply: bool) -> None:
    async with AsyncSessionLocal() as session:
        result = await repair_notification_and_task_data(session, apply=apply)
        if apply:
            await session.commit()
        else:
            await session.rollback()
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair notification states and legacy manual-task ownership")
    parser.add_argument("--apply", action="store_true", help="persist changes; omit for mandatory dry-run")
    args = parser.parse_args()
    asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    main()
