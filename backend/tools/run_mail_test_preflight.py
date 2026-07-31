from __future__ import annotations

import asyncio
import json

from app.services.mail_test_preflight import MailTestPreflightError, run_mail_test_preflight


async def main() -> None:
    try:
        result = await run_mail_test_preflight()
    except MailTestPreflightError as exc:
        result = exc.result
    print(json.dumps(result, ensure_ascii=False, default=str))
    if result.get("status") != "passed" or result.get("messages_sent") != 0:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
