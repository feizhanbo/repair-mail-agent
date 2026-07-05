from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import users as user_service


class FakeSession:
    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user
        self.executed = []
        self.added = []
        self.deleted = []
        self.committed = False

    async def get(self, _model, user_id: int):
        return self.user if self.user.id == user_id else None

    async def execute(self, statement):
        self.executed.append(statement)

    async def delete(self, instance) -> None:
        self.deleted.append(instance)

    def add(self, instance) -> None:
        self.added.append(instance)

    async def commit(self) -> None:
        self.committed = True


def make_user(user_id: int = 12) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        username="codex_operator_validation",
        real_name="Codex Validation",
        email="codex.validation@example.local",
        phone=None,
        department="Validation",
        status="active",
        last_login_at=None,
        created_at=None,
        updated_at=None,
    )


@pytest.mark.anyio
async def test_delete_user_succeeds_when_only_user_roles_exist(monkeypatch) -> None:
    async def fake_roles(_session, user_id: int):
        assert user_id == 12
        return ["operator"]

    async def fake_references(_session, user_id: int):
        assert user_id == 12
        return []

    monkeypatch.setattr(user_service, "_roles_for_user", fake_roles)
    monkeypatch.setattr(user_service, "_user_reference_summary", fake_references)
    session = FakeSession(make_user())

    result = await user_service.delete_user(session, user_id=12, operator_user_id=7)

    assert result["deleted"] is True
    assert result["user"]["roles"] == ["operator"]
    assert session.executed
    assert session.deleted == [session.user]
    assert session.added
    assert session.committed is True


@pytest.mark.anyio
async def test_delete_user_rejects_business_references(monkeypatch) -> None:
    references = [{"table": "operation_logs", "field": "user_id", "count": 1}]

    async def fake_references(_session, user_id: int):
        assert user_id == 12
        return references

    monkeypatch.setattr(user_service, "_user_reference_summary", fake_references)
    session = FakeSession(make_user())

    with pytest.raises(HTTPException) as exc_info:
        await user_service.delete_user(session, user_id=12, operator_user_id=7)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {"code": "USER_HAS_REFERENCES", "data": {"references": references}}
    assert session.executed == []
    assert session.deleted == []
    assert session.committed is False
