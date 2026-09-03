from __future__ import annotations

import asyncio
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import seed as seed_data
from app.config import settings
from app.models import Email, EmailThread, ParseResult, RepairTicket, ReplyRecord, ReplyTemplate
from app.services import emails, mail_test_preflight, replies
from app.services.mail_safety import test_envelope_allowed as envelope_allowed
from app.services.mail_safety import test_mail_configuration_reasons as configuration_reasons


def run_async(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def test_test_mail_boundary_requires_exact_accounts_and_single_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "IMAP_HOST", "imap.accotest.com")
    monkeypatch.setattr(settings, "IMAP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "IMAP_PASSWORD", "secret")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.accotest.com")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(settings, "SMTP_RECIPIENT_WHITELIST", ["rmatest2@accotest.com"])

    assert configuration_reasons() == []
    assert envelope_allowed("rmatest2@accotest.com", None) is True
    assert envelope_allowed("rmatest2@accotest.com", "copy@accotest.com") is False
    assert envelope_allowed("outside@example.com", None) is False


def test_smtp_preflight_logs_in_and_uses_noop_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            calls.append("connect")

        def starttls(self) -> None:
            calls.append("starttls")

        def login(self, user: str, password: str) -> None:
            calls.append("login")

        def noop(self) -> tuple[int, bytes]:
            calls.append("noop")
            return 250, b"ok"

        def quit(self) -> None:
            calls.append("quit")

    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.accotest.com")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(mail_test_preflight.smtplib, "SMTP", FakeSMTP)

    result = mail_test_preflight._smtp_login_preflight()

    assert result["messages_sent"] == 0
    assert result["stage"] == "complete"
    assert calls == ["connect", "starttls", "login", "noop", "quit"]


def test_smtp_preflight_returns_masked_stage_and_stable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class DisconnectedSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            raise mail_test_preflight.smtplib.SMTPServerDisconnected("sensitive provider response")

    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.accotest.com")
    monkeypatch.setattr(settings, "SMTP_PORT", 465)
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(mail_test_preflight.smtplib, "SMTP_SSL", DisconnectedSMTP)

    with pytest.raises(mail_test_preflight.SmtpPreflightStageError) as caught:
        mail_test_preflight._smtp_login_preflight()

    assert caught.value.result == {
        "status": "failed",
        "host": "***.accotest.com",
        "account": "rm***@accotest.com",
        "tls": True,
        "stage": "tls",
        "error_code": "SMTP_SERVER_DISCONNECTED",
        "authenticated": False,
        "noop": False,
        "messages_sent": 0,
    }


@pytest.mark.parametrize(
    ("ordinary_enabled", "followup_enabled", "rma_attachment_enabled"),
    [
        (False, False, False),
        (False, False, True),
        (False, True, False),
        (False, True, True),
        (True, False, False),
        (True, False, True),
        (True, True, False),
        (True, True, True),
    ],
)
def test_all_three_switch_combinations_keep_ordinary_and_followup_independent(
    monkeypatch: pytest.MonkeyPatch,
    ordinary_enabled: bool,
    followup_enabled: bool,
    rma_attachment_enabled: bool,
) -> None:
    monkeypatch.setattr(settings, "AUTO_SEND_ENABLED", ordinary_enabled)
    monkeypatch.setattr(settings, "AUTO_FOLLOWUP_ENABLED", followup_enabled)
    monkeypatch.setattr(settings, "RMA_AUTO_SEND_ENABLED", rma_attachment_enabled)
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "SMTP_RECIPIENT_WHITELIST", ["rmatest2@accotest.com"])
    ordinary = ReplyRecord(reply_type="receipt", to_addresses="rmatest2@accotest.com", cc_addresses=None)
    followup = ReplyRecord(reply_type="missing_fields", to_addresses="rmatest2@accotest.com", cc_addresses=None)

    assert replies._reply_can_auto_send(ordinary) is ordinary_enabled
    assert replies._reply_can_auto_send(followup) is followup_enabled


def test_seed_closes_rma_sent_only_after_issue_and_archive_evidence() -> None:
    direct_close_rules = [
        transition
        for transition in seed_data.WORKFLOW_TRANSITIONS
        if transition["from_status_code"] == "ready_for_export" and transition["to_status_code"] == "closed"
    ]
    rma_send_rules = [
        transition
        for transition in seed_data.WORKFLOW_TRANSITIONS
        if transition["from_status_code"] == "ready_for_export" and transition["to_status_code"] == "rma_sent"
    ]
    close_rules = [
        transition
        for transition in seed_data.WORKFLOW_TRANSITIONS
        if transition["from_status_code"] == "rma_sent" and transition["to_status_code"] == "closed"
    ]
    assert direct_close_rules == []
    assert [transition["trigger_event"] for transition in rma_send_rules] == ["rma_reply_sent"]
    assert [transition["trigger_event"] for transition in close_rules] == ["rma_issued_and_archived"]


@run_async
async def test_unlinked_business_reply_creates_isolated_open_manual_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent_type = "customer_supplement"
    email = Email(id=1, mailbox_account="rmatest1@accotest.com", message_id=f"<{intent_type}@accotest.com>")
    parse_result = ParseResult(id=2, email_id=1, parser_type="ai", intent_type=intent_type)
    ticket = RepairTicket(id=3, ticket_no="RMA-ORPHAN", current_status_code="manual_review")
    ensure = AsyncMock(return_value=ticket)
    monkeypatch.setattr(emails, "ensure_manual_review_ticket_from_parse_result", ensure)

    result = await emails._create_orphan_review_ticket(
        SimpleNamespace(),
        email=email,
        parse_result=parse_result,
    )

    assert result is not None
    assert result[0] is ticket
    assert email.parse_status == "needs_manual"
    assert parse_result.apply_status == "needs_manual_review"
    assert parse_result.accepted is False
    assert ensure.await_args.kwargs["task_type"] == f"{intent_type}_orphaned"


@run_async
async def test_subject_similarity_never_queries_for_existing_thread() -> None:
    session = SimpleNamespace(
        scalar=AsyncMock(),
        get=AsyncMock(),
        add=Mock(),
        flush=AsyncMock(),
    )

    thread = await emails._find_thread_for_email(
        session,
        message_id="<new-message@accotest.com>",
        in_reply_to=None,
        references_header=None,
        normalized_subject="same repair subject",
        from_domain="customer.example",
    )

    session.scalar.assert_not_awaited()
    assert thread.merge_confidence == 1.0
    assert "no exact" in thread.merge_reason


@run_async
async def test_reply_to_closed_ticket_reuses_rfc_thread_without_reopening_ticket() -> None:
    parent = Email(id=5, thread_id=9, message_id="<closed-source@accotest.com>", mailbox_account="test")
    old_thread = EmailThread(id=9, thread_key="old", ticket_id=11)
    closed_ticket = RepairTicket(id=11, ticket_no="RMA-CLOSED", current_status_code="closed")

    async def get(model, object_id, **kwargs):
        if model is EmailThread and object_id == 9:
            return old_thread
        if model is RepairTicket and object_id == 11:
            return closed_ticket
        return None

    session = SimpleNamespace(scalar=AsyncMock(return_value=parent), get=get, add=Mock(), flush=AsyncMock())
    resolved_thread = await emails._find_thread_for_email(
        session,
        message_id="<new-after-close@accotest.com>",
        in_reply_to="<closed-source@accotest.com>",
        references_header=None,
        normalized_subject="same repair subject",
        from_domain="accotest.com",
    )

    assert resolved_thread is old_thread
    assert resolved_thread.ticket_id == 11


@run_async
async def test_uncertain_followup_count_changes_only_after_confirmed_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = ReplyRecord(
        id=7,
        ticket_id=1,
        reply_type="missing_fields",
        to_addresses="rmatest2@accotest.com",
        send_status="send_uncertain",
    )
    ticket = RepairTicket(
        id=1,
        ticket_no="RMA2026072002",
        current_status_code="need_customer_info",
        followup_count=0,
        max_followup_count=3,
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[reply, ticket]),
        scalar=AsyncMock(return_value=None),
    )
    transition = AsyncMock()
    monkeypatch.setattr(replies, "transition_ticket", transition)
    monkeypatch.setattr(replies, "log_operation", AsyncMock())

    result = await replies.reconcile_uncertain_reply(
        session,
        reply_id=7,
        user_id=2,
        outcome="sent",
        reason="已核对测试邮箱",
    )

    assert result["send_status"] == "sent"
    assert ticket.followup_count == 1
    transition.assert_awaited_once()


@run_async
async def test_delivery_manual_task_does_not_destroy_ready_for_export(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = RepairTicket(id=1, ticket_no="RMA-READY", current_status_code="ready_for_export")
    create_task = AsyncMock()
    transition = AsyncMock()
    monkeypatch.setattr(replies, "create_manual_task_if_missing", create_task)
    monkeypatch.setattr(replies, "transition_ticket", transition)

    await replies._ensure_reply_manual_task(
        SimpleNamespace(),
        ticket=ticket,
        task_type="reply_send_uncertain",
        reason="SMTP_SEND_RESULT_UNCERTAIN",
        email_id=7,
        user_id=2,
    )

    create_task.assert_awaited_once()
    transition.assert_not_awaited()
    assert ticket.current_status_code == "ready_for_export"
