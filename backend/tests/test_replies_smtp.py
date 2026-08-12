from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import replies


class FakeSMTP:
    def __init__(self, calls: list[tuple], label: str, host: str, port: int, timeout: int) -> None:
        self.calls = calls
        self.label = label
        calls.append((label, host, port, timeout))

    def __enter__(self) -> "FakeSMTP":
        self.calls.append(("enter", self.label))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.calls.append(("exit", self.label))

    def starttls(self) -> None:
        self.calls.append(("starttls", self.label))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", self.label, user, bool(password)))

    def send_message(self, message) -> None:
        self.calls.append(("send_message", self.label, message["To"], message["From"]))


def _configure_smtp(monkeypatch: pytest.MonkeyPatch, *, port: int) -> None:
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_PORT", port)
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "smtp-password")
    monkeypatch.setattr(settings, "SMTP_RECIPIENT_WHITELIST", ["rmatest2@accotest.com"])


def _reply() -> SimpleNamespace:
    return SimpleNamespace(
        to_addresses="rmatest2@accotest.com",
        cc_addresses=None,
        subject="Repair update",
        in_reply_to=None,
        references_header=None,
        final_body="Your repair request has been received.",
        draft_body=None,
    )


def test_followup_headers_keep_original_mail_thread() -> None:
    assert replies._reply_subject("Re: Repair request SN001", "RMA001") == "Re: Repair request SN001"
    assert replies._message_id_chain(
        "<root@example.com> <older@example.com>",
        "<current@example.com>",
        "<root@example.com>",
    ) == "<root@example.com> <older@example.com> <current@example.com>"


def test_rma_message_is_a_thread_reply_even_with_business_rma_subject() -> None:
    reply = SimpleNamespace(
        to_addresses="rmatest2@accotest.com",
        cc_addresses=None,
        subject="RMA2026070910南京矽力微电子技术有限公司",
        in_reply_to="<latest-customer@example.com>",
        references_header=(
            "<original-repair@example.com> "
            "<customer-supplement@example.com> "
            "<latest-customer@example.com>"
        ),
        final_body="template body with return address",
        draft_body=None,
    )

    message = replies._build_reply_message(
        reply,
        "<rma-reply@accotest.com>",
        attachment_content=b"%PDF-1.7",
        attachment_filename="RMA2026070910南京矽力微电子技术有限公司.pdf",
    )

    # The test-only transport prefix is a safety gate; the stored business
    # subject and attachment basename remain identical.
    assert message["Subject"] == "[TEST ONLY] RMA2026070910南京矽力微电子技术有限公司"
    assert message["In-Reply-To"] == "<latest-customer@example.com>"
    assert message["References"] == (
        "<original-repair@example.com> "
        "<customer-supplement@example.com> "
        "<latest-customer@example.com>"
    )
    assert next(message.iter_attachments()).get_filename() == (
        "RMA2026070910南京矽力微电子技术有限公司.pdf"
    )


def test_send_reply_uses_smtp_ssl_for_port_465(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    _configure_smtp(monkeypatch, port=465)

    monkeypatch.setattr(
        replies.smtplib,
        "SMTP_SSL",
        lambda host, port, timeout: FakeSMTP(calls, "ssl", host, port, timeout),
    )
    monkeypatch.setattr(
        replies.smtplib,
        "SMTP",
        lambda *args, **kwargs: pytest.fail("SMTP should not be used for port 465"),
    )

    ok, message_id, error = replies._send_reply_via_smtp(_reply())

    assert ok is True
    assert message_id
    assert error is None
    assert ("ssl", "smtp.example.com", 465, 20) in calls
    assert not any(call[0] == "starttls" for call in calls)


def test_send_reply_keeps_starttls_for_port_587(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    _configure_smtp(monkeypatch, port=587)

    monkeypatch.setattr(
        replies.smtplib,
        "SMTP",
        lambda host, port, timeout: FakeSMTP(calls, "smtp", host, port, timeout),
    )
    monkeypatch.setattr(
        replies.smtplib,
        "SMTP_SSL",
        lambda *args, **kwargs: pytest.fail("SMTP_SSL should not be used for port 587"),
    )

    ok, message_id, error = replies._send_reply_via_smtp(_reply())

    assert ok is True
    assert message_id
    assert error is None
    assert ("smtp", "smtp.example.com", 587, 20) in calls
    actions = [call[0] for call in calls]
    assert "starttls" in actions
    assert actions.index("starttls") < actions.index("login")


def test_send_reply_uses_the_prebuilt_archived_message_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    sent_messages: list[object] = []
    _configure_smtp(monkeypatch, port=465)

    class CapturingSMTP(FakeSMTP):
        def send_message(self, message) -> None:
            sent_messages.append(message)
            super().send_message(message)

    monkeypatch.setattr(
        replies.smtplib,
        "SMTP_SSL",
        lambda host, port, timeout: CapturingSMTP(calls, "ssl", host, port, timeout),
    )
    reply = _reply()
    reply.id = 44
    message_id = replies._smtp_message_id(reply)
    archived_message = replies._build_reply_message(reply, message_id)
    archived_bytes = archived_message.as_bytes()

    ok, returned_message_id, error = replies._send_reply_via_smtp(
        reply,
        message=archived_message,
    )

    assert ok is True
    assert error is None
    assert returned_message_id == message_id
    assert sent_messages == [archived_message]
    assert sent_messages[0].as_bytes() == archived_bytes


def test_send_reply_blocks_non_whitelisted_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_smtp(monkeypatch, port=465)
    reply = _reply()
    reply.to_addresses = "outside@example.com"

    monkeypatch.setattr(
        replies.smtplib,
        "SMTP_SSL",
        lambda *args, **kwargs: pytest.fail("SMTP_SSL should not be used for non-whitelisted recipients"),
    )

    ok, message_id, error = replies._send_reply_via_smtp(reply)

    assert ok is False
    assert message_id is None
    assert error


@pytest.mark.parametrize(
    ("to_addresses", "cc_addresses"),
    [
        ("rmatest2@accotest.com, outside@example.com", None),
        ("rmatest2@accotest.com", "outside@example.com"),
    ],
)
def test_send_reply_requires_every_recipient_to_be_whitelisted(
    monkeypatch: pytest.MonkeyPatch,
    to_addresses: str,
    cc_addresses: str | None,
) -> None:
    _configure_smtp(monkeypatch, port=465)
    reply = _reply()
    reply.to_addresses = to_addresses
    reply.cc_addresses = cc_addresses
    monkeypatch.setattr(
        replies.smtplib,
        "SMTP_SSL",
        lambda *args, **kwargs: pytest.fail("SMTP must not be called when any recipient is outside the whitelist"),
    )

    ok, message_id, error = replies._send_reply_via_smtp(reply)

    assert ok is False
    assert message_id is None
    assert error == "SMTP_TEST_ENVELOPE_INVALID"
