from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from email.utils import parseaddr
from typing import Any, Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def clamp_page_size(page_size: int) -> int:
    return max(1, min(page_size, 100))


async def paginate_scalars(session: AsyncSession, statement: Select[Any], page: int, page_size: int) -> tuple[list[Any], int]:
    page_no = max(1, page)
    size = clamp_page_size(page_size)
    count_statement = select(func.count()).select_from(statement.order_by(None).limit(None).offset(None).subquery())
    total = int(await session.scalar(count_statement) or 0)
    result = await session.execute(statement.offset((page_no - 1) * size).limit(size))
    return list(result.scalars().all()), total


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_message_id(message_id: str | None, *, fallback_hash: str | None = None) -> str:
    if message_id and message_id.strip():
        return message_id.strip()
    if fallback_hash and fallback_hash.strip():
        return f"<raw-{fallback_hash.strip().lower()[:24]}@repair-mail-agent.local>"
    return f"<manual-{sha256_text(str(utcnow().timestamp()))[:24]}@repair-mail-agent.local>"


def normalize_subject(subject: str | None) -> str | None:
    if not subject:
        return None
    normalized = subject.strip().lower()
    normalized = re.sub(r"^(\s*(re|fw|fwd|回复|转发)\s*[:：]\s*)+", "", normalized, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", normalized)[:500]


def address_domain(address: str | None) -> str | None:
    if not address:
        return None
    parsed = parseaddr(address)[1] or address
    if "@" not in parsed:
        return None
    return parsed.rsplit("@", 1)[-1].lower()


def to_plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, tuple):
        return tuple(to_plain(item) for item in value)
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    return value


def model_to_dict(instance: Any, fields: Sequence[str]) -> dict[str, Any]:
    return {field: to_plain(getattr(instance, field)) for field in fields}
