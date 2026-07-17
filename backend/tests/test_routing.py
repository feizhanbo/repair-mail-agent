from __future__ import annotations

import pytest

from app.config import settings
from app.models import Email, User
from app.services.routing import choose_system_owner, detect_language


class Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def first(self):
        return self.values[0] if self.values else None


class Session:
    def __init__(self, preferred):
        self.preferred = preferred

    async def scalar(self, _statement):
        return self.preferred

    async def execute(self, _statement):
        return Result([])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _email(body: str) -> Email:
    return Email(mailbox_account="test", message_id=f"<{hash(body)}@example.com>", from_address="customer@example.com", clean_body=body)


@pytest.mark.anyio
async def test_language_codes_drive_miya_and_demi_routes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ROUTING_DOMESTIC_USERNAME", "miya")
    monkeypatch.setattr(settings, "ROUTING_FOREIGN_USERNAME", "demi")

    zh_owner, zh_language, zh_reason = await choose_system_owner(Session(User(id=11, username="miya", status="active")), _email("设备报修"))
    en_owner, en_language, en_reason = await choose_system_owner(Session(User(id=12, username="demi", status="active")), _email("Repair request"))

    assert (zh_owner, zh_language) == (11, "zh-CN")
    assert "username:miya" in zh_reason
    assert (en_owner, en_language) == (12, "en-US")
    assert "username:demi" in en_reason


def test_unknown_language_uses_domestic_fallback_code() -> None:
    assert detect_language(_email("123456")) == "unknown"
