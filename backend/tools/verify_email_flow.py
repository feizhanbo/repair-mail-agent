from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings
from app.core.database import AsyncSessionLocal
from app.integrations.ai_provider import AiExtractResponse
from app.models import ParseResult
from app.schemas.business import EmailIngestRequest
from app.services import emails as email_service
from app.services.ai import _enrich_ai_quality
from app.services.email_flow_trace import build_email_flow_trace
from app.services.eml import payload_from_eml_bytes
from app.services.parser import classify_email, extract_fields
from app.services.runtime_config import read_runtime_config


def _mask_url_password(url: str) -> str:
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", url)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _payload_from_eml(path: Path, *, mailbox_account: str, folder_name: str | None) -> EmailIngestRequest:
    return payload_from_eml_bytes(path.read_bytes(), mailbox_account=mailbox_account, folder_name=folder_name)


def _offline_report(payload: EmailIngestRequest) -> dict[str, Any]:
    email = SimpleNamespace(
        id=None,
        subject=payload.subject,
        text_body=payload.text_body,
        html_body=payload.html_body,
        clean_body=payload.text_body,
        latest_reply_segment=payload.text_body,
        from_address=payload.from_address,
        in_reply_to=payload.in_reply_to,
        references_header=payload.references_header,
    )
    intent_type, confidence, reason = classify_email(email, payload.text_body or "")
    extracted = extract_fields(email)
    return {
        "mode": "dry_run",
        "writes_database": False,
        "runtime_config": {
            "database_url": _mask_url_password(settings.DATABASE_URL),
            "ai_configured": bool(settings.AI_API_KEY),
            "smtp_configured": bool(settings.SMTP_PASSWORD and settings.SMTP_HOST and settings.SMTP_HOST != "smtp.example.com"),
            "reply_send_mode": settings.REPLY_SEND_MODE,
            "auto_send_enabled": settings.AUTO_SEND_ENABLED,
            "confidence_threshold": settings.CONFIDENCE_THRESHOLD,
        },
        "email": {
            "message_id": payload.message_id,
            "in_reply_to": payload.in_reply_to,
            "references_header": payload.references_header,
            "from_address": payload.from_address,
            "to_addresses": payload.to_addresses,
            "cc_addresses": payload.cc_addresses,
            "subject": payload.subject,
            "sent_at": payload.sent_at,
            "received_at": payload.received_at,
            "text_body_length": len(payload.text_body or ""),
            "html_body_length": len(payload.html_body or ""),
        },
        "attachments": [
            {
                "file_name": attachment.get("file_name"),
                "content_type": attachment.get("content_type"),
                "file_size": attachment.get("file_size"),
                "is_inline": attachment.get("is_inline"),
                "parse_status": attachment.get("parse_status"),
            }
            for attachment in payload.attachments
        ],
        "rule_classification": {
            "intent_type": intent_type,
            "confidence": confidence,
            "reason": reason,
        },
        "rule_parse": {
            "fields": extracted["fields"],
            "items": extracted["items"],
            "missing_fields": extracted["missing_fields"],
            "conflict_fields": extracted["conflict_fields"],
            "confidence_score": extracted["confidence_score"],
            "field_confidences": extracted["field_confidences"],
            "evidence": extracted["evidence"],
        },
        "next_required_steps": [
            "Start the configured database and run with --confirm-write to verify persistence, ticket links, manual tasks, replies, and status logs.",
            "Configure AI_API_KEY in the runtime environment to verify the mandatory LLM parse path.",
            "Configure SMTP settings before verifying final outbound send.",
        ],
    }


def _install_mock_ai(mock_ai_output: dict[str, Any]) -> None:
    async def fake_create_ai_parse_candidate(
        session,
        *,
        email,
        attachments,
        mode,
        ticket_id=None,
        rule_context=None,
    ):
        del attachments, rule_context
        parsed = AiExtractResponse.model_validate(mock_ai_output)
        parsed = await _enrich_ai_quality(session, parsed=parsed, email=email)
        parse_result = ParseResult(
            email_id=email.id,
            ticket_id=ticket_id,
            parser_type="ai",
            parser_version=f"mock:{settings.AI_PROMPT_VERSION}",
            intent_type=parsed.intent_type,
            extracted_fields=parsed.extracted_fields,
            extracted_items={"items": parsed.extracted_items},
            missing_fields=parsed.missing_fields,
            conflict_fields=parsed.conflict_fields,
            confidence_score=parsed.confidence_score,
            field_confidences=parsed.field_confidences,
            evidence={
                **parsed.evidence,
                "source_type": "ai",
                "provider": "mock",
                "model": "mock",
                "mode": mode,
            },
            apply_status="pending",
        )
        session.add(parse_result)
        await session.flush()
        return {"parse_result": parse_result, "ai_call_log": None}

    email_service.create_ai_parse_candidate = fake_create_ai_parse_candidate


async def run(args: argparse.Namespace) -> int:
    read_runtime_config()
    if args.mock_ai_output:
        _install_mock_ai(_load_json(args.mock_ai_output))

    if args.dry_run:
        payload = (
            _payload_from_eml(args.eml, mailbox_account=args.mailbox_account, folder_name=args.folder_name)
            if args.eml
            else EmailIngestRequest.model_validate(_load_json(args.sample))
        )
        report = _offline_report(payload)
        rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0

    async with AsyncSessionLocal() as session:
        if args.email_id is not None:
            if args.reparse_existing:
                await email_service.reparse_email(
                    session,
                    email_id=args.email_id,
                    user_id=args.user_id,
                    reason="验证工具触发已有邮件重新解析。",
                )
                await session.commit()
            report = await build_email_flow_trace(session, email_id=args.email_id, include_database_url=True)
        else:
            payload = (
                _payload_from_eml(args.eml, mailbox_account=args.mailbox_account, folder_name=args.folder_name)
                if args.eml
                else EmailIngestRequest.model_validate(_load_json(args.sample))
            )
            try:
                ingest_result = await email_service.ingest_email(session, payload=payload, user_id=args.user_id, auto_parse=not args.no_auto_parse)
                await session.commit()
                email_id = ingest_result.get("email", {}).get("id")
                if not email_id:
                    report = {"database_url": settings.DATABASE_URL, "ingest_result": ingest_result, "error": "EMAIL_ID_NOT_RETURNED"}
                else:
                    report = await build_email_flow_trace(session, email_id=email_id, ingest_result=ingest_result, include_database_url=True)
            except Exception:
                await session.rollback()
                raise

    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a real email sample through the repair-mail business flow and print a database trace report.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sample", type=Path, help="Path to an EmailIngestRequest JSON sample. Writes to the configured database.")
    source.add_argument("--eml", type=Path, help="Path to a raw .eml sample. Writes to the configured database.")
    source.add_argument("--email-id", type=int, help="Trace an existing email id without writing.")
    parser.add_argument("--mock-ai-output", type=Path, help="Optional AiExtractResponse JSON. When set, no external AI call is made for parsing.")
    parser.add_argument("--output", type=Path, help="Optional report output JSON path.")
    parser.add_argument("--user-id", type=int, default=None, help="Optional operator user id used for operation logs.")
    parser.add_argument("--mailbox-account", default="manual-eml", help="Mailbox account used when ingesting --eml.")
    parser.add_argument("--folder-name", default="INBOX", help="Folder name used when ingesting --eml.")
    parser.add_argument("--no-auto-parse", action="store_true", help="Only ingest the email; do not run parsing.")
    parser.add_argument("--dry-run", action="store_true", help="Parse the sample without database writes. Valid only with --sample or --eml.")
    parser.add_argument("--reparse-existing", action="store_true", help="Reparse an existing --email-id before printing the trace. Requires --confirm-write.")
    parser.add_argument("--confirm-write", action="store_true", help="Required. This script writes to the configured database.")
    args = parser.parse_args(argv)
    if args.email_id is not None and args.dry_run:
        parser.error("--dry-run is only valid with --sample or --eml.")
    if args.reparse_existing and args.email_id is None:
        parser.error("--reparse-existing is only valid with --email-id.")
    if args.reparse_existing and not args.confirm_write:
        parser.error("--confirm-write is required when using --reparse-existing because this script writes to the configured database.")
    if args.email_id is None and not args.dry_run and not args.confirm_write:
        parser.error("--confirm-write is required when using --sample or --eml because this script writes to the configured database.")
    if args.email_id is not None and args.mock_ai_output:
        parser.error("--mock-ai-output is only valid with --sample or --eml.")
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv or sys.argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main())
