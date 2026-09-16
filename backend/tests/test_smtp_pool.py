from __future__ import annotations

from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from app.services import smtp_pool


class FakeSMTP:
    instances = 0
    sent = 0
    noops = 0
    raw_messages: list[tuple[str, tuple[str, ...], bytes]] = []

    def __init__(self, *_args, **_kwargs) -> None:
        type(self).instances += 1
        self.sock = None

    def starttls(self) -> None:
        return None

    def login(self, _user: str, _password: str) -> None:
        return None

    def noop(self):
        type(self).noops += 1
        return 250, b"OK"

    def send_message(self, _message):
        type(self).sent += 1
        return {}

    def sendmail(self, from_address: str, recipients: list[str], raw_message: bytes):
        type(self).raw_messages.append((from_address, tuple(recipients), raw_message))
        return {}

    def quit(self) -> None:
        return None


def _message() -> EmailMessage:
    message = EmailMessage()
    message["From"] = "repair@example.com"
    message["To"] = "customer@example.com"
    message.set_content("test")
    return message


def test_connection_is_reused_then_rotated_at_message_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.instances = FakeSMTP.sent = FakeSMTP.noops = 0
    monkeypatch.setattr(smtp_pool.settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(smtp_pool.settings, "SMTP_PORT", 587)
    monkeypatch.setattr(smtp_pool.settings, "SMTP_USER", "repair@example.com")
    monkeypatch.setattr(smtp_pool.settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(smtp_pool.settings, "SMTP_MAX_CONNECTIONS", 1)
    monkeypatch.setattr(smtp_pool.settings, "SMTP_MESSAGES_PER_CONNECTION", 2)
    monkeypatch.setattr(smtp_pool.settings, "SMTP_RATE_LIMIT_PER_MINUTE", 100)
    monkeypatch.setattr(smtp_pool.smtplib, "SMTP", FakeSMTP)
    smtp_pool.reset_smtp_connection_pool()

    pool = smtp_pool.smtp_connection_pool()
    pool.send_message(_message())
    pool.send_message(_message())
    assert FakeSMTP.instances == 1
    pool.send_message(_message())
    assert FakeSMTP.instances == 2
    assert FakeSMTP.sent == 3
    assert FakeSMTP.noops >= 1
    smtp_pool.reset_smtp_connection_pool()


def test_single_connection_is_never_used_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    class BlockingSMTP(FakeSMTP):
        def send_message(self, _message):
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with active_lock:
                active -= 1
            return {}

    monkeypatch.setattr(smtp_pool.settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(smtp_pool.settings, "SMTP_PORT", 587)
    monkeypatch.setattr(smtp_pool.settings, "SMTP_USER", "repair@example.com")
    monkeypatch.setattr(smtp_pool.settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(smtp_pool.settings, "SMTP_MAX_CONNECTIONS", 1)
    monkeypatch.setattr(smtp_pool.settings, "SMTP_MESSAGES_PER_CONNECTION", 20)
    monkeypatch.setattr(smtp_pool.settings, "SMTP_RATE_LIMIT_PER_MINUTE", 100)
    monkeypatch.setattr(smtp_pool.smtplib, "SMTP", BlockingSMTP)
    smtp_pool.reset_smtp_connection_pool()

    pool = smtp_pool.smtp_connection_pool()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _index: pool.send_message(_message()), range(4)))

    assert max_active == 1
    smtp_pool.reset_smtp_connection_pool()


def test_frozen_rfc822_bytes_are_passed_to_sendmail_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.raw_messages = []
    monkeypatch.setattr(smtp_pool.settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(smtp_pool.settings, "SMTP_PORT", 587)
    monkeypatch.setattr(smtp_pool.settings, "SMTP_USER", "repair@example.com")
    monkeypatch.setattr(smtp_pool.settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(smtp_pool.settings, "SMTP_MAX_CONNECTIONS", 1)
    monkeypatch.setattr(smtp_pool.settings, "SMTP_MESSAGES_PER_CONNECTION", 20)
    monkeypatch.setattr(smtp_pool.settings, "SMTP_RATE_LIMIT_PER_MINUTE", 100)
    monkeypatch.setattr(smtp_pool.smtplib, "SMTP", FakeSMTP)
    smtp_pool.reset_smtp_connection_pool()
    raw = b"From: repair@example.com\r\nTo: customer@example.com\r\n\r\nexact-body\r\n"

    smtp_pool.smtp_connection_pool().send_raw(
        from_address="repair@example.com",
        recipients=["customer@example.com"],
        raw_message=raw,
    )

    assert FakeSMTP.raw_messages == [
        ("repair@example.com", ("customer@example.com",), raw)
    ]
    smtp_pool.reset_smtp_connection_pool()
