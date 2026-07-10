from __future__ import annotations

import argparse
import asyncio
import imaplib
import json
import sys
import time
from email.parser import BytesParser
from email.policy import default as default_policy
from pathlib import Path
from typing import Any

from sqlalchemy import select

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.models import EmailTicketLink, ReplyRecord
from app.services import replies as reply_service
from app.services.email_flow_trace import build_email_flow_trace
from app.services.eml import payload_from_eml_bytes
from app.services.imap_fetcher import fetch_imap_emails
from app.services.runtime_config import read_runtime_config


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _connect_imap() -> imaplib.IMAP4_SSL:
    client = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT, timeout=30)
    client.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
    return client


def _append_eml_to_inbox(raw: bytes) -> str:
    client = _connect_imap()
    try:
        typ, data = client.append("INBOX", None, None, raw)
        if typ != "OK":
            raise RuntimeError("IMAP_APPEND_FAILED")
        return " ".join(part.decode("utf-8", errors="ignore") if isinstance(part, bytes) else str(part) for part in data)
    finally:
        client.logout()


def _fetch_headers_for_uid(client: imaplib.IMAP4_SSL, uid: bytes) -> dict[str, str | None]:
    typ, data = client.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM TO DATE)])")
    if typ != "OK":
        return {}
    for item in data:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            message = BytesParser(policy=default_policy).parsebytes(item[1])
            return {
                "uid": uid.decode("ascii", errors="ignore"),
                "message_id": str(message.get("Message-ID")) if message.get("Message-ID") else None,
                "subject": str(message.get("Subject")) if message.get("Subject") else None,
                "from": str(message.get("From")) if message.get("From") else None,
                "to": str(message.get("To")) if message.get("To") else None,
                "date": str(message.get("Date")) if message.get("Date") else None,
            }
    return {}


def _wait_for_imap_message(message_id: str, *, folder_name: str = "INBOX", timeout_seconds: int = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_seen: list[dict[str, str | None]] = []
    while time.monotonic() < deadline:
        client = _connect_imap()
        try:
            typ, _ = client.select(folder_name, readonly=True)
            if typ != "OK":
                raise RuntimeError("IMAP_SELECT_FAILED")
            typ, data = client.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
            if typ == "OK" and data and data[0]:
                uids = data[0].split()
                return {"found": True, "message": _fetch_headers_for_uid(client, uids[-1])}

            typ, data = client.uid("SEARCH", None, "FROM", settings.SMTP_USER)
            if typ == "OK" and data and data[0]:
                last_seen = [_fetch_headers_for_uid(client, uid) for uid in data[0].split()[-5:]]
                for item in last_seen:
                    if item.get("message_id") == message_id:
                        return {"found": True, "message": item}
        finally:
            try:
                client.close()
            except Exception:
                pass
            client.logout()
        time.sleep(5)
    return {"found": False, "last_messages_from_sender": last_seen}


def _summarize_trace(trace: dict[str, Any]) -> dict[str, Any]:
    email = trace.get("email") or {}
    return {
        "email": {
            "id": email.get("id"),
            "message_id": email.get("message_id"),
            "subject": email.get("subject"),
            "parse_status": email.get("parse_status"),
            "intent_type": email.get("intent_type"),
            "imap_uid": email.get("imap_uid"),
            "fetch_job_run_id": email.get("fetch_job_run_id"),
            "raw_eml_oss_object_id": email.get("raw_eml_oss_object_id"),
        },
        "attachments": [
            {
                "id": item.get("id"),
                "file_name": item.get("file_name"),
                "content_type": item.get("content_type"),
                "file_size": item.get("file_size"),
                "oss_object_id": item.get("oss_object_id"),
                "parse_status": item.get("parse_status"),
            }
            for item in trace.get("attachments", [])
        ],
        "parse_results": [
            {
                "id": item.get("id"),
                "parser_type": item.get("parser_type"),
                "intent_type": item.get("intent_type"),
                "confidence_score": item.get("confidence_score"),
                "missing_fields": item.get("missing_fields"),
                "conflict_fields": item.get("conflict_fields"),
                "apply_status": item.get("apply_status"),
            }
            for item in trace.get("parse_results", [])
        ],
        "tickets": [
            {
                "id": item.get("id"),
                "ticket_no": item.get("ticket_no"),
                "current_status_code": item.get("current_status_code"),
                "confidence_score": item.get("confidence_score"),
                "assigned_user_id": item.get("assigned_user_id"),
            }
            for item in trace.get("tickets", [])
        ],
        "manual_review_tasks": [
            {
                "id": item.get("id"),
                "task_type": item.get("task_type"),
                "status": item.get("status"),
                "priority": item.get("priority"),
                "assigned_user_id": item.get("assigned_user_id"),
                "trigger_reason": item.get("trigger_reason"),
            }
            for item in trace.get("manual_review_tasks", [])
        ],
        "reply_records": [
            {
                "id": item.get("id"),
                "ticket_id": item.get("ticket_id"),
                "reply_type": item.get("reply_type"),
                "generate_source": item.get("generate_source"),
                "review_status": item.get("review_status"),
                "send_status": item.get("send_status"),
                "to_addresses": item.get("to_addresses"),
                "smtp_message_id": item.get("smtp_message_id"),
                "outgoing_email_id": item.get("outgoing_email_id"),
                "error_message": item.get("error_message"),
            }
            for item in trace.get("reply_records", [])
        ],
        "ai_call_logs": [
            {
                "id": item.get("id"),
                "call_type": item.get("call_type"),
                "status": item.get("status"),
                "confidence_score": item.get("confidence_score"),
                "error_message": item.get("error_message"),
            }
            for item in trace.get("ai_call_logs", [])
        ],
        "notification_count": len(trace.get("notification_events", [])),
    }


async def _latest_reply_for_email(session, email_id: int) -> ReplyRecord | None:
    direct = await session.scalar(select(ReplyRecord).where(ReplyRecord.related_email_id == email_id).order_by(ReplyRecord.id.desc()))
    if direct is not None:
        return direct
    ticket_ids = select(EmailTicketLink.ticket_id).where(EmailTicketLink.email_id == email_id)
    return await session.scalar(select(ReplyRecord).where(ReplyRecord.ticket_id.in_(ticket_ids)).order_by(ReplyRecord.id.desc()))


async def run(args: argparse.Namespace) -> int:
    read_runtime_config()
    raw = args.eml.read_bytes()
    payload = payload_from_eml_bytes(raw, mailbox_account=settings.IMAP_USER, folder_name="INBOX")
    report: dict[str, Any] = {
        "sample": {"path": str(args.eml), "message_id": payload.message_id, "subject": payload.subject, "attachment_count": len(payload.attachments)},
        "append": None,
        "fetch": None,
        "trace": None,
        "send": None,
        "imap_received": None,
    }

    if args.append_to_imap:
        if not args.confirm_imap_append:
            raise SystemExit("--confirm-imap-append is required with --append-to-imap")
        report["append"] = {"status": "ok", "imap_response": _append_eml_to_inbox(raw)}

    async with AsyncSessionLocal() as session:
        fetch_result = await fetch_imap_emails(
            session,
            folder_name="INBOX",
            limit=args.limit,
            unseen_only=False,
            message_id=payload.message_id,
            auto_parse=not args.no_auto_parse,
            archive_to_oss=not args.no_oss,
            user_id=args.user_id,
        )
        await session.commit()
        report["fetch"] = fetch_result
        email_id = None
        for item in fetch_result.get("fetched", []):
            if item.get("email_id"):
                email_id = item["email_id"]
        if email_id is not None:
            trace = await build_email_flow_trace(session, email_id=email_id)
            report["trace"] = _summarize_trace(trace)

        if args.approve_send:
            if not args.confirm_send:
                raise SystemExit("--confirm-send is required with --approve-send")
            if email_id is None:
                raise RuntimeError("EMAIL_ID_NOT_AVAILABLE")
            reply = await _latest_reply_for_email(session, email_id)
            if reply is None:
                report["send"] = {"status": "skipped", "reason": "NO_REPLY_RECORD"}
            else:
                reply.to_addresses = args.test_recipient
                reply.cc_addresses = None
                if reply.subject and not reply.subject.startswith("[E2E] "):
                    reply.subject = f"[E2E] {reply.subject}"[:500]
                send_result = await reply_service.approve_reply(session, reply_id=reply.id, user_id=args.user_id)
                await session.commit()
                sent_reply = send_result.get("reply", {})
                report["send"] = {
                    "reply_id": sent_reply.get("id"),
                    "review_status": sent_reply.get("review_status"),
                    "send_status": sent_reply.get("send_status"),
                    "smtp_message_id": sent_reply.get("smtp_message_id"),
                    "outgoing_email_id": sent_reply.get("outgoing_email_id"),
                    "error_message": sent_reply.get("error_message"),
                    "sent_to": args.test_recipient,
                }
                if sent_reply.get("smtp_message_id"):
                    report["imap_received"] = _wait_for_imap_message(sent_reply["smtp_message_id"], timeout_seconds=args.poll_seconds)
                trace = await build_email_flow_trace(session, email_id=email_id)
                report["trace"] = _summarize_trace(trace)

    _print_json(report)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify live IMAP -> OSS -> parse -> SMTP -> IMAP flow with one EML sample.")
    parser.add_argument("--eml", type=Path, required=True)
    parser.add_argument("--append-to-imap", action="store_true")
    parser.add_argument("--confirm-imap-append", action="store_true")
    parser.add_argument("--approve-send", action="store_true")
    parser.add_argument("--confirm-send", action="store_true")
    parser.add_argument("--test-recipient", default=settings.IMAP_USER)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--no-auto-parse", action="store_true")
    parser.add_argument("--no-oss", action="store_true")
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    try:
        return await run(parse_args(argv or sys.argv[1:]))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
