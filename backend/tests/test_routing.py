from __future__ import annotations

import pytest

from app.models import Email, User
from app.services.routing import choose_scope_owner, choose_system_owner, detect_language


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
async def test_language_does_not_assign_owner_before_customer_scope() -> None:
    zh_owner, zh_language, zh_reason = await choose_system_owner(Session(None), _email("设备报修"))
    en_owner, en_language, en_reason = await choose_system_owner(Session(None), _email("Repair request"))

    assert (zh_owner, zh_language) == (None, "zh-CN")
    assert "customer_scope:unresolved" in zh_reason
    assert (en_owner, en_language) == (None, "en-US")
    assert "customer_scope:unresolved" in en_reason


@pytest.mark.anyio
async def test_customer_scope_drives_miya_and_demi_routes() -> None:
    miya = User(id=11, username="miya", status="active")
    demi = User(id=12, username="demi", status="active")
    assert (await choose_scope_owner(Session(miya), "domestic")).id == 11
    assert (await choose_scope_owner(Session(demi), "overseas")).id == 12


def test_unknown_language_uses_domestic_fallback_code() -> None:
    assert detect_language(_email("123456")) == "unknown"


@pytest.mark.anyio
async def test_unknown_scope_does_not_randomly_fallback() -> None:
    assert await choose_scope_owner(Session(User(id=99, username="other", status="active")), None) is None
