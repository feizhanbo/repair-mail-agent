from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1 import emails as email_api
from app.models import Email, EmailAttachment


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    def __init__(self, instance) -> None:
        self.instance = instance
        self.added = []
        self.committed = False

    async def get(self, model, _id: int):
        if isinstance(self.instance, model):
            return self.instance
        return None

    def add(self, value) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.anyio
async def test_raw_eml_download_url_returns_presigned_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    email = Email(
        mailbox_account="manual",
        message_id="<raw@example.com>",
        from_address="customer@example.com",
        raw_eml_oss_object_id=11,
    )
    email.id = 5

    async def fake_generate(_session, *, oss_object_id: int, expires_seconds: int) -> str:
        assert oss_object_id == 11
        assert expires_seconds == 120
        return "https://oss.example.com/signed"

    monkeypatch.setattr(email_api, "generate_presigned_url_for_object", fake_generate)

    response = await email_api.raw_eml_download_url(5, FakeSession(email), SimpleNamespace(id=7), expires_seconds=120)

    assert response["success"] is True
    assert response["data"] == {
        "object_id": 11,
        "file_name": "email-5.eml",
        "url": "https://oss.example.com/signed",
        "expires_seconds": 120,
    }


@pytest.mark.anyio
async def test_attachment_download_url_returns_presigned_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    attachment = EmailAttachment(email_id=5, file_name="fault.txt", parse_status="pending", oss_object_id=22)
    attachment.id = 9

    async def fake_generate(_session, *, oss_object_id: int, expires_seconds: int) -> str:
        assert oss_object_id == 22
        assert expires_seconds == 300
        return "https://oss.example.com/attachment"

    monkeypatch.setattr(email_api, "generate_presigned_url_for_object", fake_generate)

    response = await email_api.attachment_download_url(9, FakeSession(attachment), SimpleNamespace(id=7), expires_seconds=300)

    assert response["success"] is True
    assert response["data"] == {
        "attachment_id": 9,
        "object_id": 22,
        "file_name": "fault.txt",
        "url": "https://oss.example.com/attachment",
        "expires_seconds": 300,
    }
