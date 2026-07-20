from __future__ import annotations

from email.utils import getaddresses, parseaddr

from app.config import settings


TEST_MAIL_SENDER = "rmatest1@accotest.com"
TEST_MAIL_RECIPIENT = "rmatest2@accotest.com"


def normalized_address(value: str | None) -> str:
    return parseaddr(value or "")[1].strip().lower()


def configured_test_whitelist() -> set[str]:
    return {
        normalized_address(value)
        for value in settings.SMTP_RECIPIENT_WHITELIST
        if normalized_address(value)
    }


def test_mail_configuration_reasons() -> list[str]:
    reasons: list[str] = []
    if normalized_address(settings.IMAP_USER) != TEST_MAIL_SENDER:
        reasons.append("IMAP_LOGIN_MUST_BE_RMATEST1")
    if normalized_address(settings.SMTP_USER) != TEST_MAIL_SENDER:
        reasons.append("SMTP_LOGIN_MUST_BE_RMATEST1")
    if configured_test_whitelist() != {TEST_MAIL_RECIPIENT}:
        reasons.append("SMTP_WHITELIST_MUST_CONTAIN_ONLY_RMATEST2")
    if not settings.IMAP_HOST or not settings.IMAP_PASSWORD:
        reasons.append("IMAP_NOT_CONFIGURED")
    if not settings.SMTP_HOST or not settings.SMTP_PASSWORD:
        reasons.append("SMTP_NOT_CONFIGURED")
    if settings.SMTP_PORT not in {465, 587}:
        reasons.append("SMTP_TLS_PORT_REQUIRED")
    return reasons


def test_envelope_allowed(to_addresses: str | None, cc_addresses: str | None) -> bool:
    recipients = [address.lower() for _, address in getaddresses([to_addresses or ""]) if address]
    cc = [address.lower() for _, address in getaddresses([cc_addresses or ""]) if address]
    return (
        recipients == [TEST_MAIL_RECIPIENT]
        and not cc
        and configured_test_whitelist() == {TEST_MAIL_RECIPIENT}
        and normalized_address(settings.SMTP_USER) == TEST_MAIL_SENDER
    )


def test_only_subject(value: str | None) -> str:
    subject = (value or "").strip()
    return subject if subject.upper().startswith("[TEST ONLY]") else f"[TEST ONLY] {subject}".strip()
