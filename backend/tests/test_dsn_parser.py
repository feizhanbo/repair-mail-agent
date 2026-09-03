from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from app.models import MailDeliveryEvent, ManualReviewTask
from app.services.dsn_parser import parse_dsn, persist_dsn_event


def _dsn(*, action: str, status: str) -> bytes:
    report = EmailMessage()
    report["From"] = "MAILER-DAEMON@example.com"
    report["To"] = "repair@example.com"
    report["Subject"] = "Delivery Status Notification"
    report.set_type("multipart/report")
    report.set_param("report-type", "delivery-status")

    human = EmailMessage()
    human.set_content("Delivery failed")
    report.attach(human)

    delivery = EmailMessage()
    delivery.set_type("message/delivery-status")
    block = EmailMessage()
    block["Original-Message-ID"] = "<reply-42@example.com>"
    block["Final-Recipient"] = "rfc822; customer@example.com"
    block["Action"] = action
    block["Status"] = status
    block["Diagnostic-Code"] = "smtp; mailbox unavailable"
    delivery.set_payload([block])
    report.attach(delivery)
    return report.as_bytes()


def test_parse_hard_bounce() -> None:
    result = parse_dsn(_dsn(action="failed", status="5.1.1"))
    assert result is not None
    assert result.delivery_status == "hard_bounce"
    assert result.original_message_id == "<reply-42@example.com>"
    assert result.final_recipient == "rfc822; customer@example.com"


def test_parse_soft_bounce() -> None:
    result = parse_dsn(_dsn(action="delayed", status="4.2.0"))
    assert result is not None
    assert result.delivery_status == "soft_bounce"


def test_regular_mail_is_not_dsn() -> None:
    message = EmailMessage()
    message["From"] = "customer@example.com"
    message["To"] = "repair@example.com"
    message.set_content("normal repair mail")
    assert parse_dsn(message.as_bytes()) is None


@pytest.mark.anyio
async def test_hard_bounce_creates_delivery_event_and_task_without_reopening_ticket() -> None:
    outbox = SimpleNamespace(id=9, ticket_id=88)

    class Session:
        def __init__(self) -> None:
            self.added = []
            self.scalar_calls = 0

        async def scalar(self, _statement):
            self.scalar_calls += 1
            return outbox if self.scalar_calls == 1 else None

        def add(self, item) -> None:
            self.added.append(item)

        async def flush(self) -> None:
            for index, item in enumerate(self.added, start=1):
                if getattr(item, "id", None) is None:
                    item.id = index

    session = Session()
    event = await persist_dsn_event(
        session,
        raw_eml=_dsn(action="failed", status="5.1.1"),
        raw_sha256="a" * 64,
    )

    assert isinstance(event, MailDeliveryEvent)
    assert event.ticket_id == 88
    assert event.delivery_status == "hard_bounce"
    task = next(item for item in session.added if isinstance(item, ManualReviewTask))
    assert task.ticket_id == 88
    assert task.task_type == "mail_delivery_hard_bounce"
