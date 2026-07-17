from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Email, Role, User, UserRole


CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def detect_language(email: Email) -> str:
    sample = "\n".join(
        value
        for value in (email.subject, email.latest_reply_segment, email.clean_body, email.text_body)
        if value
    )[:12000]
    if CJK_PATTERN.search(sample):
        return "zh-CN"
    if re.search(r"[A-Za-z]", sample):
        return "en-US"
    return "unknown"


async def choose_system_owner(session: AsyncSession, email: Email) -> tuple[int | None, str, str]:
    language = detect_language(email)
    preferred_username = settings.ROUTING_FOREIGN_USERNAME if language == "en-US" else settings.ROUTING_DOMESTIC_USERNAME
    base = (
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(User.status == "active", Role.role_code == "operator")
    )
    preferred = await session.scalar(base.where(User.username == preferred_username))
    if preferred is not None:
        return preferred.id, language, f"language:{language};username:{preferred_username}"

    fallback = (await session.execute(base.order_by(User.id))).scalars().first()
    if fallback is not None:
        return fallback.id, language, f"language:{language};fallback:{fallback.username}"
    return None, language, f"language:{language};operator_unavailable"
