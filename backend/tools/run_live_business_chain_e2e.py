from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from sqlalchemy import or_, select, text

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.models import (
    AiCallLog,
    Email,
    ExportSap,
    JobRunLog,
    MailFetchRecord,
    OssObject,
    RepairTicket,
    RepairTicketItem,
    ReplyRecord,
    TicketRelayExport,
    TicketRma,
    WorkerLease,
)
from app.services.gold_replay import (
    apply_gold_test_reset,
    plan_gold_test_reset,
    verify_gold_test_reset,
)
from app.services.mail_safety import (
    TEST_MAIL_RECIPIENT,
    TEST_MAIL_SENDER,
    test_mail_configuration_reasons,
)
from app.services.mail_test_preflight import REQUIRED_DATABASE_REVISION
from app.services.storage import download_oss_object_bytes, oss_object_exists
from tools.run_gold_mail_regression import (
    _wait_for_parse_terminal,
    _exclusive_suite_run,
    _fetch_raw_by_message_id,
    _relay_control,
    _relay_reset,
    _rmatest1_max_uid,
    _rmatest1_new_messages,
    _rmatest2_max_uid,
    _rmatest2_new_messages,
    run_database_async,
)
from tools.run_new_repair_mail_e2e import (
    Client,
    current_config,
    find_email,
    patch_config,
    validate_complete_path,
    wait_for_job,
    wait_for_ticket,
    wait_until,
)
from tools.run_rmatest_batch_e2e import (
    apply_temporary_master_data,
    cleanup_temporary_master_data,
)


PROJECT_ROOT = BACKEND_ROOT.parent
RESULT_ROOT = PROJECT_ROOT / "test-results" / "live-business-chain"
EXPECTED_SEND_COUNT = 1
EXPECTED_REVISION = "e8z3a4b5c6d7"
LIVE_MANIFEST_PATH = BACKEND_ROOT / "tools" / "manifests" / "live_business_chain_e2e_manifest.json"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
FORBIDDEN_PROVIDER_MARKERS = {"cache", "gold", "fake", "fixture", "stub", "mock", "local"}
REQUIRED_TEXT_CALL_TYPES = {"mail_classification", "repair_field_extract"}
SAFE_CONFIG_KEYS = (
    "auto_send_enabled",
    "auto_followup_enabled",
    "rma_auto_send_enabled",
    "relay_sqlserver_enabled",
)


class LiveChainError(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveChainError("PLAN_READ_FAILED", details={"path": str(path)}) from exc
    if not isinstance(value, dict):
        raise LiveChainError("PLAN_FORMAT_INVALID")
    return value


def canonical_hash(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("plan_hash", None)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _run_root(run_id: str) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise LiveChainError("RUN_ID_INVALID")
    return RESULT_ROOT / run_id


def build_plan(run_id: str, *, created_at: str | None = None) -> dict[str, Any]:
    source = _read_json(LIVE_MANIFEST_PATH)
    if source.get("schema_version") != 1:
        raise LiveChainError("LIVE_MANIFEST_SCHEMA_INVALID")
    selected = source.get("message") if isinstance(source.get("message"), dict) else {}
    gold = source.get("gold") if isinstance(source.get("gold"), dict) else {}
    header = selected.get("header") if isinstance(selected.get("header"), dict) else {}
    fallback = source.get("sap_fallback") if isinstance(source.get("sap_fallback"), dict) else {}
    message_id = str(selected.get("message_id") or "")
    if not re.fullmatch(r"<[^<>\s]+@[^<>\s]+>", message_id):
        raise LiveChainError("LIVE_MANIFEST_MESSAGE_ID_INVALID")
    if (
        str(source.get("source_mailbox") or "").lower() != TEST_MAIL_SENDER
        or str(source.get("outbound_recipient_only") or "").lower() != TEST_MAIL_RECIPIENT
        or int(source.get("expected_smtp_send_count") or 0) != EXPECTED_SEND_COUNT
        or str(header.get("from") or "").lower() != TEST_MAIL_RECIPIENT
        or str(header.get("to") or "").lower() != TEST_MAIL_SENDER
        or bool(str(header.get("cc") or "").strip())
        or bool(selected.get("attachments"))
    ):
        raise LiveChainError("LIVE_MANIFEST_MAIL_SCOPE_INVALID")
    temporary_keys = (
        "temporary_sn_assets",
        "temporary_board_cards",
        "temporary_customer_policies",
    )
    if any(gold.get(key) for key in temporary_keys):
        raise LiveChainError("LIVE_MANIFEST_MASTER_DATA_OVERRIDE_FORBIDDEN")
    if (
        not re.fullmatch(r"\d{10}", str(fallback.get("rma_no") or ""))
        or not str(fallback.get("call_id") or "").isdecimal()
    ):
        raise LiveChainError("LIVE_MANIFEST_SAP_FALLBACK_INVALID")
    expected_items = list(gold.get("expected_items") or [])
    expected_board_items = list(gold.get("expected_board_items") or [])
    if not expected_items or len(expected_items) != len(expected_board_items):
        raise LiveChainError("FIXED_GOLD_ITEMS_INVALID")
    manifest = {
        "batch_id": f"live-business-chain-{run_id}",
        "messages": [{"message_id": message_id, "gold": gold}],
    }
    plan: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": created_at or now_iso(),
        "expected_database_revision": EXPECTED_REVISION,
        "expected_smtp_send_count": EXPECTED_SEND_COUNT,
        "scenario": str(source.get("scenario_id") or "fixed_manifest_imap"),
        "inbound": {
            "source": "existing_imap",
            "message_id": message_id,
            "from": TEST_MAIL_RECIPIENT,
            "to": [TEST_MAIL_SENDER],
            "cc": [],
            "bcc": [],
            "subject": str(header.get("subject") or ""),
            "raw_sha256": str(selected.get("raw_sha256") or ""),
            "source_uid": str(selected.get("uid") or ""),
            "uid_validity": str(selected.get("uid_validity") or ""),
            "attachment_count": len(selected.get("attachments") or []),
        },
        "expected_outbound": {
            "from": TEST_MAIL_SENDER,
            "to": [TEST_MAIL_RECIPIENT],
            "cc": [],
            "bcc": [],
            "subject_prefix": "[TEST ONLY]",
            "reply_count": 1,
        },
        "master_data_manifest": manifest,
        "expected": {
            "intent_type": gold.get("expected_intent"),
            "fields": gold.get("expected_final_fields") or gold.get("expected_fields") or {},
            "items": expected_items,
            "board_items": expected_board_items,
            "rma_no": str(fallback.get("rma_no") or ""),
            "call_id_fallback": str(fallback.get("call_id") or ""),
        },
        "egress": source.get("egress") or {},
        "cleanup": source.get("cleanup") or {},
    }
    plan["plan_hash"] = canonical_hash(plan)
    return plan


def validate_plan(plan: dict[str, Any], expected_hash: str, confirm_run_id: str) -> None:
    actual_hash = canonical_hash(plan)
    if plan.get("plan_hash") != actual_hash or expected_hash != actual_hash:
        raise LiveChainError("PLAN_HASH_MISMATCH", details={"actual_plan_hash": actual_hash})
    if plan.get("run_id") != confirm_run_id:
        raise LiveChainError("RUN_ID_CONFIRMATION_MISMATCH")
    if int(plan.get("expected_smtp_send_count") or 0) != EXPECTED_SEND_COUNT:
        raise LiveChainError("SMTP_SEND_COUNT_NOT_EXACTLY_ONE")
    inbound = plan.get("inbound") or {}
    outbound = plan.get("expected_outbound") or {}
    if (
        inbound.get("from") != TEST_MAIL_RECIPIENT
        or inbound.get("to") != [TEST_MAIL_SENDER]
        or inbound.get("cc")
        or inbound.get("bcc")
        or int(inbound.get("attachment_count") or 0) != 0
        or outbound.get("from") != TEST_MAIL_SENDER
        or outbound.get("to") != [TEST_MAIL_RECIPIENT]
        or outbound.get("cc")
        or outbound.get("bcc")
        or int(outbound.get("reply_count") or 0) != 1
    ):
        raise LiveChainError("MAIL_ENVELOPE_OR_COUNT_INVALID")
    if (
        inbound.get("source") != "existing_imap"
        or not re.fullmatch(r"[0-9a-f]{64}", str(inbound.get("raw_sha256") or ""))
    ):
        raise LiveChainError("FIXED_IMAP_SOURCE_INVALID")
    if plan.get("egress") != {
        "text_ai": True,
        "oss": True,
        "attachment_ai": False,
        "real_sqlserver": False,
    }:
        raise LiveChainError("EGRESS_SCOPE_INVALID")


def _login(client: Client) -> int:
    username = (
        os.getenv("INTEGRATION_ADMIN_USERNAME", "").strip()
        or str(settings.DEFAULT_ADMIN_USERNAME or "").strip()
    )
    password = os.getenv("INTEGRATION_ADMIN_PASSWORD", "") or str(
        settings.DEFAULT_ADMIN_PASSWORD or ""
    )
    if not username or not password:
        raise LiveChainError("INTEGRATION_ADMIN_CREDENTIALS_REQUIRED")
    data = client.data("POST", "/api/v1/auth/login", body={"username": username, "password": password})
    if not isinstance(data, dict) or not data.get("access_token"):
        raise LiveChainError("LOGIN_RESPONSE_INVALID")
    roles = set((data.get("user") or {}).get("roles") or [])
    if "admin" not in roles:
        raise LiveChainError("INTEGRATION_ADMIN_ROLE_REQUIRED")
    client.token = str(data["access_token"])
    return int((data.get("user") or {}).get("id") or 0)


def _smtp_probe() -> None:
    smtp = _open_rmatest2_smtp()
    try:
        code, _ = smtp.noop()
        if int(code) != 250:
            raise LiveChainError("RMATEST2_SMTP_NOOP_FAILED")
    finally:
        try:
            smtp.quit()
        except (OSError, smtplib.SMTPException):
            pass


def _system_smtp_probe() -> None:
    if not settings.SMTP_HOST or not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        raise LiveChainError("SYSTEM_SMTP_CONFIGURATION_INCOMPLETE")
    context = ssl.create_default_context()
    if settings.SMTP_PORT == 465:
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30, context=context)
    else:
        smtp = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
        smtp.starttls(context=context)
    try:
        smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        code, _ = smtp.noop()
        if int(code) != 250:
            raise LiveChainError("SYSTEM_SMTP_NOOP_FAILED")
    finally:
        try:
            smtp.quit()
        except (OSError, smtplib.SMTPException):
            pass


def _oss_probe() -> None:
    exists = asyncio.run(
        oss_object_exists(
            bucket=settings.OSS_BUCKET,
            object_key=f"live-business-chain/doctor/nonexistent-{hashlib.sha256(now_iso().encode()).hexdigest()}",
            endpoint=settings.OSS_ENDPOINT,
        )
    )
    if exists:
        raise LiveChainError("OSS_DOCTOR_SENTINEL_UNEXPECTEDLY_EXISTS")


def _relay_probe() -> None:
    request = Request(
        f"{settings.TEST_RELAY_BASE_URL.rstrip('/')}/health",
        headers={"Authorization": f"Bearer {settings.TEST_RELAY_TOKEN}"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise LiveChainError("TEST_RELAY_HEALTH_FAILED") from exc
    if payload.get("status") != "ok":
        raise LiveChainError("TEST_RELAY_HEALTH_FAILED")


def _open_rmatest2_smtp() -> smtplib.SMTP:
    host = settings.E2E_RMATEST2_SMTP_HOST
    user = settings.E2E_RMATEST2_SMTP_USER
    password = settings.E2E_RMATEST2_SMTP_PASSWORD
    if not host or not user or not password:
        raise LiveChainError("RMATEST2_SMTP_CONFIGURATION_INCOMPLETE")
    context = ssl.create_default_context()
    if settings.E2E_RMATEST2_SMTP_USE_SSL:
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, settings.E2E_RMATEST2_SMTP_PORT, timeout=30, context=context)
    else:
        smtp = smtplib.SMTP(host, settings.E2E_RMATEST2_SMTP_PORT, timeout=30)
        smtp.starttls(context=context)
    smtp.login(user, password)
    return smtp


def build_inbound_message(plan: dict[str, Any]) -> EmailMessage:
    inbound = plan["inbound"]
    message = EmailMessage()
    message["From"] = inbound["from"]
    message["To"] = ", ".join(inbound["to"])
    message["Subject"] = inbound["subject"]
    message["Message-ID"] = inbound["message_id"]
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["X-AIRMA-Live-E2E-Run-ID"] = str(plan["run_id"])
    message.set_content(inbound["body"])
    return message


def send_inbound_once(plan: dict[str, Any], smtp_factory: Callable[[], smtplib.SMTP] = _open_rmatest2_smtp) -> dict[str, Any]:
    message = build_inbound_message(plan)
    recipients = [address.lower() for _, address in getaddresses([message.get("To", "")]) if address]
    if recipients != [TEST_MAIL_SENDER] or message.get("Cc") or message.get("Bcc"):
        raise LiveChainError("SMTP_ENVELOPE_INVALID")
    smtp = smtp_factory()
    try:
        refused = smtp.send_message(message, from_addr=TEST_MAIL_RECIPIENT, to_addrs=[TEST_MAIL_SENDER])
        if refused:
            raise LiveChainError("SMTP_RECIPIENT_REFUSED", details={"refused_count": len(refused)})
        return {"accepted": True, "message_id": message["Message-ID"], "recipient_count": 1}
    except (OSError, smtplib.SMTPException) as exc:
        # Delivery may already have been accepted. Never retry automatically.
        raise LiveChainError("SMTP_DELIVERY_UNCERTAIN_NO_RETRY", details={"exception": type(exc).__name__}) from exc
    finally:
        try:
            smtp.quit()
        except (OSError, smtplib.SMTPException):
            pass


async def _database_doctor() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        queued_rows = list(
            (
                await session.execute(
                    text(
                        "SELECT id, job_type, status FROM job_run_logs "
                        "WHERE status IN ('queued', 'running', 'retry_wait') "
                        "ORDER BY id LIMIT 20"
                    )
                )
            ).mappings().all()
        )
        lease_table_exists = bool(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name = 'worker_leases'"
                )
            )
        )
        if not lease_table_exists:
            return {
                "revision": revision,
                "required_revision": EXPECTED_REVISION,
                "revision_ready": False,
                "worker_lease_table_exists": False,
                "healthy_worker_lease_count": 0,
                "worker": None,
                "active_jobs": [dict(row) for row in queued_rows],
                "active_job_ids": [int(row["id"]) for row in queued_rows],
            }
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        leases = list((await session.execute(select(WorkerLease).where(WorkerLease.lease_expires_at > now))).scalars().all())
        return {
            "revision": revision,
            "required_revision": EXPECTED_REVISION,
            "revision_ready": revision == EXPECTED_REVISION == REQUIRED_DATABASE_REVISION,
            "worker_lease_table_exists": True,
            "healthy_worker_lease_count": len(leases),
            "worker": (
                {
                    "instance_id": leases[0].instance_id,
                    "queue_name": leases[0].queue_name,
                    "fencing_token": leases[0].fencing_token,
                    "heartbeat_at": leases[0].heartbeat_at,
                    "lease_expires_at": leases[0].lease_expires_at,
                }
                if len(leases) == 1
                else None
            ),
            "active_jobs": [dict(row) for row in queued_rows],
            "active_job_ids": [int(row["id"]) for row in queued_rows],
        }


def doctor(live: bool) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "messages_sent": 0,
        "static_mail_gate": not test_mail_configuration_reasons(),
        "real_mail_gate": bool(settings.RUN_REAL_MAIL_INTEGRATION_TESTS),
        "gold_cleanup_gate": bool(settings.E2E_GOLD_RUN_ENABLED),
        "rmatest1_identity": settings.IMAP_USER.lower() == TEST_MAIL_SENDER,
        "rmatest2_identity": settings.E2E_RMATEST2_IMAP_USER.lower() == TEST_MAIL_RECIPIENT,
        "text_ai_configured": bool(settings.AI_API_KEY and settings.AI_MODEL),
        "oss_configured": bool(settings.OSS_ENDPOINT and settings.OSS_BUCKET and settings.OSS_ACCESS_KEY and settings.OSS_SECRET_KEY),
        "relay_is_test_http": settings.RELAY_ADAPTER.strip().lower() == "test_http",
        "relay_configured": bool(settings.TEST_RELAY_BASE_URL and settings.TEST_RELAY_TOKEN),
    }
    client = Client()
    try:
        _login(client)
        health = client.request("GET", "/health")
        checks["api"] = health
        config = current_config(client)
        integrations = config.get("integrations") or {}
        provider_identity = " ".join(
            str(integrations.get(key) or "") for key in ("text_ai_provider", "ai_model")
        ).lower()
        checks["api_text_ai_configured"] = bool(integrations.get("text_ai_configured"))
        checks["api_text_ai_is_real"] = bool(provider_identity) and not any(
            marker in provider_identity for marker in FORBIDDEN_PROVIDER_MARKERS
        )
    except Exception as exc:
        checks["api_error"] = type(exc).__name__
    try:
        checks["database"] = run_database_async(_database_doctor())
    except Exception as exc:
        checks["database"] = {"error": type(exc).__name__}
    if live:
        try:
            _fetch_raw_by_message_id(
                host=settings.IMAP_HOST,
                port=settings.IMAP_PORT,
                user=settings.IMAP_USER,
                password=settings.IMAP_PASSWORD,
                folder=settings.IMAP_FOLDER,
                message_id="<airma-doctor-never-exists@accotest.com>",
                use_ssl=True,
            )
        except Exception as exc:
            # A zero-match proves authentication/select/search worked.
            checks["rmatest1_imap"] = "ready" if getattr(exc, "code", "") == "IMAP_MESSAGE_ID_MATCH_COUNT_INVALID" else type(exc).__name__
        try:
            _fetch_raw_by_message_id(
                host=settings.E2E_RMATEST2_IMAP_HOST,
                port=settings.E2E_RMATEST2_IMAP_PORT,
                user=settings.E2E_RMATEST2_IMAP_USER,
                password=settings.E2E_RMATEST2_IMAP_PASSWORD,
                folder=settings.E2E_RMATEST2_IMAP_FOLDER,
                message_id="<airma-doctor-never-exists@accotest.com>",
                use_ssl=settings.E2E_RMATEST2_IMAP_USE_SSL,
            )
        except Exception as exc:
            checks["rmatest2_imap"] = "ready" if getattr(exc, "code", "") == "IMAP_MESSAGE_ID_MATCH_COUNT_INVALID" else type(exc).__name__
        try:
            _system_smtp_probe()
            checks["rmatest1_smtp"] = "ready"
        except Exception as exc:
            checks["rmatest1_smtp"] = type(exc).__name__
        try:
            _oss_probe()
            checks["oss_live"] = "ready"
        except Exception as exc:
            checks["oss_live"] = type(exc).__name__
        try:
            _relay_probe()
            checks["test_relay"] = "ready"
        except Exception as exc:
            checks["test_relay"] = type(exc).__name__
    database = checks.get("database") or {}
    passed = (
        not any(key.endswith("_error") for key in checks)
        and all(value is True for key, value in checks.items() if key not in {"messages_sent", "api", "database"} and isinstance(value, bool))
        and database.get("revision_ready") is True
        and database.get("healthy_worker_lease_count") == 1
        and not database.get("active_job_ids")
        and (
            not live
            or all(
                checks.get(key) == "ready"
                for key in ("rmatest1_imap", "rmatest2_imap", "rmatest1_smtp", "oss_live", "test_relay")
            )
        )
    )
    return {"status": "passed" if passed else "failed", "live": live, "checks": checks}


def _wait_for_inbound(message_id: str, baseline_uid: int, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        rows = [row for row in _rmatest1_new_messages(baseline_uid) if row.get("message_id") == message_id]
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise LiveChainError("INBOUND_MESSAGE_ID_NOT_UNIQUE", details={"count": len(rows)})
        time.sleep(2)
    raise LiveChainError("INBOUND_IMAP_TIMEOUT")


def _assert_message_absent_from_mailbox(
    *, host: str, port: int, user: str, password: str, folder: str, message_id: str, use_ssl: bool
) -> None:
    try:
        _fetch_raw_by_message_id(
            host=host,
            port=port,
            user=user,
            password=password,
            folder=folder,
            message_id=message_id,
            use_ssl=use_ssl,
        )
    except Exception as exc:
        if getattr(exc, "code", "") == "IMAP_MESSAGE_ID_MATCH_COUNT_INVALID" and (getattr(exc, "details", {}) or {}).get("match_count") == 0:
            return
        raise
    raise LiveChainError("MESSAGE_ID_ALREADY_IN_MAILBOX")


def _fetch_exact_job(client: Client, message_id: str) -> tuple[int, dict[str, Any]]:
    result = client.data(
        "POST",
        "/api/v1/emails/fetch/jobs",
        params={"folder_name": settings.IMAP_FOLDER, "limit": 1, "unseen_only": "false", "message_id": message_id, "auto_parse": "true"},
    )
    if not isinstance(result, dict) or result.get("reused"):
        raise LiveChainError("IMAP_FETCH_JOB_NOT_EXCLUSIVE")
    job_id = int((result.get("job") or {}).get("id") or 0)
    if not job_id:
        raise LiveChainError("IMAP_FETCH_JOB_MISSING")
    return job_id, wait_for_job(client, job_id)


def _set_relay_enabled(client: Client, enabled: bool) -> None:
    patch_config(client, relay_sqlserver_enabled=enabled)
    client.data(
        "PATCH",
        "/api/v1/system/sn-sync/config",
        body={"relay_sqlserver_enabled": enabled},
    )


def validate_pre_relay_business_data(
    email_detail: dict[str, Any],
    ticket_detail: dict[str, Any] | None,
    expected: dict[str, Any],
) -> dict[str, Any]:
    email = email_detail.get("email") or {}
    ticket_detail = ticket_detail or {}
    ticket = ticket_detail.get("ticket") or {}
    actual_items = ticket_detail.get("items") or []
    issues: list[str] = []
    if email.get("intent_type") != expected.get("intent_type"):
        issues.append("INTENT_MISMATCH")
    if not ticket:
        issues.append("TICKET_MISSING")
    if ticket and ticket.get("current_status_code") != "ready_for_export":
        issues.append("PRE_RELAY_STATUS_NOT_READY_FOR_EXPORT")
    if ticket.get("missing_fields"):
        issues.append("MISSING_FIELDS_NOT_EMPTY")
    for key, value in (expected.get("fields") or {}).items():
        if ticket.get(key) != value:
            issues.append(f"FIELD_MISMATCH:{key}")
    expected_items = expected.get("items") or []
    expected_board_items = expected.get("board_items") or []
    if len(actual_items) != len(expected_items):
        issues.append("ITEM_COUNT_MISMATCH")
    for wanted in expected_items:
        if not any(all(row.get(key) == value for key, value in wanted.items()) for row in actual_items):
            issues.append(f"ITEM_MISMATCH:{wanted.get('sn')}")
    for wanted in expected_board_items:
        if not any(all(row.get(key) == value for key, value in wanted.items()) for row in actual_items):
            issues.append(f"BOARD_ITEM_MISMATCH:{wanted.get('sn')}")
    if issues:
        raise LiveChainError(
            "BUSINESS_DATA_VALIDATION_REQUIRES_CONFIRMATION",
            details={"stage": "pre_relay", "issues": sorted(set(issues))},
        )
    return {
        "stage": "pre_relay",
        "status": "passed",
        "email_id": email.get("id"),
        "ticket_id": ticket.get("id"),
        "item_count": len(actual_items),
        "manifest_item_count": len(expected_items),
    }


async def _validate_pre_relay_ai(message_id: str) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        email = await session.scalar(select(Email).where(Email.message_id == message_id))
        if email is None:
            raise LiveChainError("DATABASE_EMAIL_MISSING")
        ticket = await session.scalar(select(RepairTicket).where(RepairTicket.source_email_id == email.id))
        fetch_record_ids = list(
            (await session.execute(select(MailFetchRecord.id).where(MailFetchRecord.message_id == message_id))).scalars().all()
        )
        rows = list(
            (
                await session.execute(
                    select(AiCallLog)
                    .where(
                        or_(
                            AiCallLog.email_id == email.id,
                            AiCallLog.ticket_id == (ticket.id if ticket else -1),
                            AiCallLog.mail_fetch_record_id.in_(fetch_record_ids or [-1]),
                        )
                    )
                    .order_by(AiCallLog.id)
                )
            ).scalars().all()
        )
        return validate_real_ai_calls(
            [{column.name: getattr(row, column.name) for column in AiCallLog.__table__.columns} for row in rows]
        )


def _wait_outbound(message_id: str, baseline_uid: int, timeout_seconds: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    matches: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        matches = [
            row
            for row in _rmatest2_new_messages(baseline_uid)
            if row.get("in_reply_to") == message_id or message_id in str(row.get("references") or "")
        ]
        if len(matches) == 1:
            return matches
        if len(matches) > 1:
            raise LiveChainError("OUTBOUND_REPLY_COUNT_EXCEEDED", details={"count": len(matches)})
        time.sleep(2)
    raise LiveChainError("OUTBOUND_IMAP_TIMEOUT", details={"count": len(matches)})


class WorkerEvidenceObserver:
    """Capture ephemeral ownership fields before successful jobs clear them."""

    def __init__(self) -> None:
        self.since = datetime.now(timezone.utc).replace(tzinfo=None)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._rows: dict[int, dict[str, Any]] = {}
        self._error: str | None = None
        self._thread = threading.Thread(target=self._run, name="live-e2e-worker-evidence", daemon=True)

    async def _observe_loop(self) -> None:
        try:
            while not self._stop.is_set():
                async with AsyncSessionLocal() as session:
                    rows = list(
                        (
                            await session.execute(
                                select(JobRunLog).where(
                                    JobRunLog.created_at >= self.since,
                                    JobRunLog.status == "running",
                                    JobRunLog.locked_by.is_not(None),
                                    JobRunLog.fencing_token.is_not(None),
                                )
                            )
                        ).scalars().all()
                    )
                with self._lock:
                    for row in rows:
                        self._rows[row.id] = {
                            "job_id": row.id,
                            "job_type": row.job_type,
                            "locked_by": row.locked_by,
                            "fencing_token": row.fencing_token,
                            "locked_at": row.locked_at,
                        }
                await asyncio.sleep(0.05)
        finally:
            await engine.dispose()

    def _run(self) -> None:
        try:
            asyncio.run(self._observe_loop())
        except Exception as exc:
            self._error = type(exc).__name__

    def start(self) -> None:
        self._thread.start()

    def finish(self) -> list[dict[str, Any]]:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise LiveChainError("WORKER_EVIDENCE_OBSERVER_STOP_TIMEOUT")
        if self._error:
            raise LiveChainError("WORKER_EVIDENCE_OBSERVER_FAILED", details={"error": self._error})
        with self._lock:
            return list(self._rows.values())


def validate_real_ai_calls(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    successful = {str(row.get("call_type")): row for row in rows if row.get("status") == "success"}
    missing = sorted(REQUIRED_TEXT_CALL_TYPES - set(successful))
    if missing:
        raise LiveChainError("REQUIRED_TEXT_AI_CALLS_MISSING", details={"call_types": missing})
    evidence: list[dict[str, Any]] = []
    for call_type in sorted(REQUIRED_TEXT_CALL_TYPES):
        row = successful[call_type]
        identity = " ".join(str(row.get(key) or "") for key in ("provider_name", "model_name", "route_name")).lower()
        if not row.get("provider_name") or not row.get("model_name") or any(marker in identity for marker in FORBIDDEN_PROVIDER_MARKERS):
            raise LiveChainError("NON_REAL_TEXT_AI_PROVIDER", details={"call_type": call_type})
        evidence.append({key: row.get(key) for key in ("id", "call_type", "provider_name", "model_name", "route_name", "route_attempt", "fallback_used", "status", "latency_ms", "total_tokens", "trace_id")})
    return evidence


def validate_oss_payload(
    *, label: str, upload_status: str, file_size: int | None, stored_sha256: str | None, payload: bytes, exists: bool
) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    if upload_status != "success" or not exists or digest != stored_sha256 or len(payload) != file_size:
        raise LiveChainError("OSS_CONTENT_VERIFICATION_FAILED", details={"object_type": label})
    if label == "rma_pdf" and not payload.startswith(b"%PDF-"):
        raise LiveChainError("RMA_PDF_FORMAT_INVALID")
    return digest


def validate_pdf_part_no(
    snapshot: dict[str, Any] | None,
    call_id: str | list[str] | tuple[str, ...] | None,
) -> str | list[str]:
    value = snapshot if isinstance(snapshot, dict) else {}
    items = value.get("items") or []
    actual = [str(item.get("part_no") or "") for item in items]
    expected = [str(item) for item in call_id] if isinstance(call_id, (list, tuple)) else [str(call_id or "")]
    if not actual or sorted(actual) != sorted(expected):
        raise LiveChainError("PDF_PART_NO_CALL_ID_MAPPING_INVALID")
    return actual[0] if len(actual) == 1 else actual


async def _collect_database_evidence(
    message_id: str,
    fetch_job_id: int,
    expected: dict[str, Any],
    worker_observations: list[dict[str, Any]],
) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        email = await session.scalar(select(Email).where(Email.message_id == message_id))
        if email is None:
            raise LiveChainError("DATABASE_EMAIL_MISSING")
        ticket = await session.scalar(select(RepairTicket).where(RepairTicket.source_email_id == email.id))
        if ticket is None or ticket.current_status_code != "rma_sent":
            raise LiveChainError("DATABASE_TICKET_NOT_RMA_SENT")
        replies = list((await session.execute(select(ReplyRecord).where(ReplyRecord.ticket_id == ticket.id))).scalars().all())
        sent_replies = [row for row in replies if row.reply_type == "rma_authorization" and row.send_status == "sent"]
        if len(sent_replies) != 1:
            raise LiveChainError("DATABASE_SENT_REPLY_COUNT_INVALID", details={"count": len(sent_replies)})
        reply = sent_replies[0]
        rmas = list((await session.execute(select(TicketRma).where(TicketRma.ticket_id == ticket.id))).scalars().all())
        if len(rmas) != 1 or rmas[0].rma_no != expected["rma_no"]:
            raise LiveChainError("RMA_IDENTITY_INVALID")
        rma = rmas[0]
        items = list((await session.execute(select(RepairTicketItem).where(RepairTicketItem.ticket_id == ticket.id))).scalars().all())
        expected_items = expected.get("items") or []
        expected_board_items = expected.get("board_items") or []
        if len(items) != len(expected_items):
            raise LiveChainError("TICKET_ITEM_COUNT_INVALID", details={"count": len(items)})
        serialized_items = [
            {
                "id": item.id,
                "sn": item.sn,
                "material_code": item.material_code,
                "material_name": item.material_name,
                "board_code": item.board_code,
                "board_name": item.board_name,
                "failure_description": item.failure_description,
                "return_location": item.return_location,
                "return_route_status": item.return_route_status,
            }
            for item in items
        ]
        for wanted in expected_items:
            if not any(all(row.get(key) == value for key, value in wanted.items()) for row in serialized_items):
                raise LiveChainError("SN_MATERIAL_MAPPING_INVALID", details={"sn": wanted.get("sn")})
        for wanted in expected_board_items:
            if not any(all(row.get(key) == value for key, value in wanted.items()) for row in serialized_items):
                raise LiveChainError("BOARD_MAPPING_INVALID", details={"sn": wanted.get("sn")})
        if any(row["return_route_status"] != "resolved" for row in serialized_items):
            raise LiveChainError("RETURN_ROUTE_INVALID")
        relay = await session.scalar(select(TicketRelayExport).where(TicketRelayExport.ticket_id == ticket.id).order_by(TicketRelayExport.id.desc()))
        sap_rows = list(
            (await session.execute(select(ExportSap).where(ExportSap.ticket_id == ticket.id).order_by(ExportSap.id))).scalars().all()
        )
        if relay is None or relay.status != "rma_received" or not sap_rows or any(row.status != "rma_received" for row in sap_rows):
            raise LiveChainError("TEST_RELAY_EVIDENCE_INVALID")
        expected_materials = sorted(str(row.get("material_code") or "") for row in expected_items)
        if (
            {row.rma_no for row in sap_rows} != {expected["rma_no"]}
            or sorted(str(row.material_code or "") for row in sap_rows) != expected_materials
            or any(not row.remote_call_id for row in sap_rows)
        ):
            raise LiveChainError("SAP_MAPPING_EVIDENCE_INVALID")
        call_ids = [str(row.remote_call_id) for row in sap_rows]
        if len(call_ids) != len(set(call_ids)) or any(not value.isdecimal() for value in call_ids):
            raise LiveChainError("SAP_CALL_ID_INVALID")
        pdf_snapshot = reply.rma_pdf_data_snapshot if isinstance(reply.rma_pdf_data_snapshot, dict) else {}
        pdf_part_no = validate_pdf_part_no(pdf_snapshot, call_ids)
        policy_snapshot = sap_rows[0].policy_snapshot if isinstance(sap_rows[0].policy_snapshot, dict) else {}
        sap_mapping = {
            "BPShipAddr": "国内" if ticket.customer_scope == "domestic" else "海外" if ticket.customer_scope == "overseas" else None,
            "BPCellular": f"{ticket.contact_person or ''}{ticket.contact_phone or ''}",
            "NAME1": policy_snapshot.get("sap_owner_name"),
            "CallID": call_ids,
            "pdf_part_no": pdf_part_no,
        }
        if sap_mapping["BPShipAddr"] != "国内" or not sap_mapping["BPCellular"]:
            raise LiveChainError("SAP_CONTACT_OR_SCOPE_MAPPING_INVALID")
        fetch_record_ids = list(
            (await session.execute(select(MailFetchRecord.id).where(MailFetchRecord.message_id == message_id))).scalars().all()
        )
        ai_rows = list(
            (
                await session.execute(
                    select(AiCallLog)
                    .where(
                        or_(
                            AiCallLog.email_id == email.id,
                            AiCallLog.ticket_id == ticket.id,
                            AiCallLog.mail_fetch_record_id.in_(fetch_record_ids or [-1]),
                        )
                    )
                    .order_by(AiCallLog.id)
                )
            ).scalars().all()
        )
        ai_evidence = validate_real_ai_calls(
            [{column.name: getattr(row, column.name) for column in AiCallLog.__table__.columns} for row in ai_rows]
        )
        object_ids = {"raw_eml": email.raw_eml_oss_object_id, "rma_pdf": rma.pdf_oss_object_id}
        if not all(object_ids.values()) or reply.rma_pdf_oss_object_id != rma.pdf_oss_object_id:
            raise LiveChainError("OSS_OBJECT_LINKS_INVALID")
        oss_evidence: dict[str, Any] = {}
        pdf_bytes = b""
        for label, object_id in object_ids.items():
            obj = await session.get(OssObject, int(object_id))
            if obj is None:
                raise LiveChainError("OSS_DATABASE_OBJECT_INVALID", details={"object_type": label})
            exists = await oss_object_exists(bucket=obj.bucket, object_key=obj.object_key, endpoint=obj.endpoint)
            payload = await download_oss_object_bytes(session, oss_object_id=obj.id)
            digest = validate_oss_payload(
                label=label,
                upload_status=obj.upload_status,
                file_size=obj.file_size,
                stored_sha256=obj.sha256_hash,
                payload=payload,
                exists=exists,
            )
            if label == "rma_pdf":
                pdf_bytes = payload
            oss_evidence[label] = {
                "id": obj.id,
                "source_type": obj.source_type,
                "upload_status": obj.upload_status,
                "file_size": obj.file_size,
                "sha256": digest,
                "exists": exists,
            }
        if rma.pdf_sha256 != oss_evidence["rma_pdf"]["sha256"]:
            raise LiveChainError("RMA_PDF_HASH_INVALID")
        if email.source_content_sha256 != oss_evidence["raw_eml"]["sha256"]:
            raise LiveChainError("RAW_EML_HASH_INVALID")
        cleanup_plan = await plan_gold_test_reset(session, message_ids=[message_id])
        job_ids = [int(value) for value in (cleanup_plan.get("resource_ids") or {}).get("jobs") or []]
        jobs = list(
            (
                await session.execute(select(JobRunLog).where(JobRunLog.id.in_(job_ids or [fetch_job_id])).order_by(JobRunLog.id))
            ).scalars().all()
        )
        failed_jobs = [{"id": job.id, "job_type": job.job_type, "status": job.status} for job in jobs if job.status != "success"]
        if failed_jobs:
            raise LiveChainError("ASSOCIATED_JOB_NOT_SUCCESSFUL", details={"jobs": failed_jobs})
        required_job_types = {"imap_fetch", "mail_ingress_process", "relay_ticket_export", "smtp_send"}
        missing_job_types = sorted(required_job_types - {job.job_type for job in jobs})
        if missing_job_types:
            raise LiveChainError("ASSOCIATED_JOB_EVIDENCE_MISSING", details={"job_types": missing_job_types})
        observation_by_id = {int(row["job_id"]): row for row in worker_observations}
        observed_required = {
            job.job_type
            for job in jobs
            if job.job_type in required_job_types and job.id in observation_by_id
        }
        missing_ownership = sorted(required_job_types - observed_required)
        if missing_ownership:
            raise LiveChainError("WORKER_FENCING_EVIDENCE_MISSING", details={"job_types": missing_ownership})
        worker_instances = {
            str(observation_by_id[job.id]["locked_by"])
            for job in jobs
            if job.id in observation_by_id
        }
        if len(worker_instances) != 1:
            raise LiveChainError("MULTIPLE_WORKERS_OBSERVED", details={"worker_count": len(worker_instances)})
        return {
            "email": {"id": email.id, "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(), "intent_type": email.intent_type, "raw_eml_sha256": email.source_content_sha256},
            "ticket": {"id": ticket.id, "ticket_no": ticket.ticket_no, "status": ticket.current_status_code, "customer_code": ticket.customer_code, "sn_validation_hash": ticket.sn_validation_hash, "safety_check_hash": ticket.safety_check_hash},
            "items": serialized_items,
            "reply": {"id": reply.id, "smtp_message_id": reply.smtp_message_id, "archive_status": reply.archive_status, "in_reply_to_sha256": hashlib.sha256(str(reply.in_reply_to).encode()).hexdigest()},
            "rma": {"id": rma.id, "rma_no": rma.rma_no, "status": rma.status, "pdf_validation_status": rma.pdf_validation_status, "pdf_archive_status": rma.pdf_archive_status, "pdf_sha256": rma.pdf_sha256},
            "relay": {"id": relay.id, "status": relay.status, "payload_hash": relay.payload_hash},
            "sap": {"row_count": len(sap_rows), "status": "rma_received", "remote_call_ids": call_ids, "rma_no": expected["rma_no"], "mapping": sap_mapping},
            "ai_calls": ai_evidence,
            "oss": oss_evidence,
            "jobs": [
                {
                    "id": job.id,
                    "job_type": job.job_type,
                    "status": job.status,
                    "attempt_count": job.attempt_count,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "locked_by": (observation_by_id.get(job.id) or {}).get("locked_by"),
                    "fencing_token": (observation_by_id.get(job.id) or {}).get("fencing_token"),
                    "locked_at": (observation_by_id.get(job.id) or {}).get("locked_at"),
                }
                for job in jobs
            ],
        }


def _validate_outbound_mail(row: dict[str, Any], message_id: str, pdf_sha256: str) -> dict[str, Any]:
    from_addresses = [address.lower() for _, address in getaddresses([str(row.get("from") or "")]) if address]
    to_addresses = [address.lower() for _, address in getaddresses([str(row.get("to") or "")]) if address]
    attachments = row.get("attachments") or []
    pdfs = [item for item in attachments if item.get("content_type") == "application/pdf"]
    if (
        from_addresses != [TEST_MAIL_SENDER]
        or to_addresses != [TEST_MAIL_RECIPIENT]
        or row.get("cc")
        or not str(row.get("subject") or "").upper().startswith("[TEST ONLY]")
        or row.get("in_reply_to") != message_id
        or message_id not in str(row.get("references") or "")
        or len(pdfs) != 1
        or pdfs[0].get("content_sha256") != pdf_sha256
    ):
        raise LiveChainError("OUTBOUND_MAIL_VALIDATION_FAILED")
    return {
        "uid": row.get("uid"),
        "message_id_sha256": hashlib.sha256(str(row.get("message_id") or "").encode()).hexdigest(),
        "thread_headers_valid": True,
        "envelope_valid": True,
        "subject_test_only": True,
        "pdf_sha256": pdfs[0]["content_sha256"],
    }


async def _cleanup_database(message_id: str, run_id: str, user_id: int, timeout_seconds: int) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        deadline = time.monotonic() + timeout_seconds
        while True:
            plan = await plan_gold_test_reset(session, message_ids=[message_id])
            if not plan.get("active_job_ids") or time.monotonic() >= deadline:
                break
            await asyncio.sleep(2)
        result: dict[str, Any] = {"plan_hash": plan["plan_hash"], "blockers": plan.get("blockers") or []}
        if result["blockers"]:
            return {**result, "status": "blocked"}
        applied = await apply_gold_test_reset(
            session,
            message_ids=[message_id],
            expected_plan_hash=plan["plan_hash"],
            suite_id="live-business-chain",
            run_id=f"cleanup-{run_id}",
            user_id=user_id,
            reason=f"Approved live business chain cleanup for {run_id}",
        )
        verification = await verify_gold_test_reset(session, message_ids=[message_id])
        return {**result, **applied, "verification": verification}


def _restore_config(client: Client, initial: dict[str, Any]) -> dict[str, Any]:
    wanted = {key: bool(initial.get(key)) for key in SAFE_CONFIG_KEYS}
    value = patch_config(client, **wanted)
    client.data(
        "PATCH",
        "/api/v1/system/sn-sync/config",
        body={"relay_sqlserver_enabled": wanted["relay_sqlserver_enabled"]},
    )
    actual = {key: bool(value.get(key)) for key in SAFE_CONFIG_KEYS}
    if actual != wanted:
        raise LiveChainError("RUNTIME_CONFIG_RESTORE_MISMATCH")
    return actual


def execute_run(plan_path: Path, expected_hash: str, confirm_run_id: str, approved_by: str, timeout_seconds: int) -> dict[str, Any]:
    plan = _read_json(plan_path)
    validate_plan(plan, expected_hash, confirm_run_id)
    if not approved_by.strip():
        raise LiveChainError("APPROVER_REQUIRED")
    root = _run_root(confirm_run_id)
    result_path = root / "result.json"
    if result_path.exists() or (root / "used.json").exists():
        raise LiveChainError("RUN_ID_ALREADY_USED")
    root.mkdir(parents=True, exist_ok=True)
    _write_json(root / "used.json", {"run_id": confirm_run_id, "plan_hash": expected_hash, "started_at": now_iso()})
    state_path = root / "temporary-master-state.json"
    master_manifest_path = root / "master-data-manifest.json"
    _write_json(master_manifest_path, plan["master_data_manifest"])
    client = Client()
    user_id = 0
    initial: dict[str, Any] | None = None
    actual_send_count = 0
    report: dict[str, Any] = {
        "run_id": confirm_run_id,
        "plan_hash": expected_hash,
        "approved_by": approved_by.strip(),
        "status": "running",
        "started_at": now_iso(),
        "actual_send_count": 0,
        "mailbox_messages_preserved": True,
        "evidence": {},
        "cleanup": {"status": "not_started"},
    }
    primary_error: Exception | None = None
    observer: WorkerEvidenceObserver | None = None
    pre_relay_validated = False
    with _exclusive_suite_run("live-business-chain", confirm_run_id):
        try:
            user_id = _login(client)
            initial = current_config(client)
            patch_config(
                client,
                auto_send_enabled=False,
                auto_followup_enabled=False,
                rma_auto_send_enabled=False,
            )
            gate = doctor(live=True)
            if gate["status"] != "passed":
                raise LiveChainError("LIVE_DOCTOR_FAILED", details={"checks": gate["checks"]})
            message_id = str(plan["inbound"]["message_id"])
            pre_cleanup = run_database_async(
                _cleanup_database(message_id, f"pre-{confirm_run_id}", user_id, timeout_seconds)
            )
            if pre_cleanup.get("status") in {"blocked", "cleanup_pending"} or not (
                pre_cleanup.get("verification") or {}
            ).get("verified"):
                raise LiveChainError("PRE_RUN_CLEANUP_INCOMPLETE")
            raw_uid, raw_eml, uid_validity = _fetch_raw_by_message_id(
                host=settings.IMAP_HOST,
                port=settings.IMAP_PORT,
                user=settings.IMAP_USER,
                password=settings.IMAP_PASSWORD,
                folder=settings.IMAP_FOLDER,
                message_id=message_id,
                use_ssl=True,
            )
            raw_sha256 = hashlib.sha256(raw_eml).hexdigest()
            if raw_sha256 != plan["inbound"]["raw_sha256"]:
                raise LiveChainError("SOURCE_EML_HASH_CHANGED")
            report["evidence"]["inbound_imap"] = {
                "uid": raw_uid,
                "uid_validity": uid_validity,
                "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
                "raw_sha256": raw_sha256,
            }
            rmatest2_baseline = _rmatest2_max_uid()
            if plan["cleanup"].get("temporary_master_data"):
                run_database_async(
                    apply_temporary_master_data(
                        plan["master_data_manifest"],
                        state_path,
                        allow_gold_e2e_snapshot_override=False,
                    )
                )
            _relay_control("normal", str(plan["expected"]["rma_no"]))
            _set_relay_enabled(client, False)
            # The independent worker reloads persisted runtime configuration on
            # a one-minute scheduler tick.  Do not enqueue the fixed mail until
            # the no-relay/no-send validation gate is guaranteed visible.
            time.sleep(65)
            observer = WorkerEvidenceObserver()
            observer.start()
            fetch_job_id, fetch_job = _fetch_exact_job(client, message_id)
            email_id = int(((fetch_job.get("result_json") or {}).get("fetched") or [{}])[0].get("email_id") or 0)
            if not email_id:
                found = wait_until(
                    f"persisted email {message_id}",
                    lambda: find_email(client, message_id),
                    lambda value: bool(value and value.get("id")),
                ) or {}
                email_id = int(found.get("id") or 0)
            if not email_id:
                raise LiveChainError("FETCHED_EMAIL_ID_MISSING")
            email_detail, ticket_detail = _wait_for_parse_terminal(
                client,
                email_id,
                timeout_seconds=timeout_seconds,
            )
            report["evidence"]["pre_relay_validation"] = validate_pre_relay_business_data(
                email_detail,
                ticket_detail,
                plan["expected"],
            )
            report["evidence"]["pre_relay_ai_calls"] = run_database_async(
                _validate_pre_relay_ai(message_id)
            )
            pre_relay_validated = True
            ticket_id = int(((ticket_detail or {}).get("ticket") or {}).get("id") or 0)
            if not ticket_id:
                raise LiveChainError("PRE_RELAY_TICKET_ID_MISSING")
            _set_relay_enabled(client, True)
            patch_config(client, auto_send_enabled=True, auto_followup_enabled=True, rma_auto_send_enabled=True)
            time.sleep(65)
            client.data("POST", f"/api/v1/tickets/{ticket_id}/validate-export")
            complete_value = wait_for_ticket(client, email_id, expected_status="rma_sent", expected_reply_type="rma_authorization")
            validate_complete_path(complete_value)
            outbound = _wait_outbound(message_id, rmatest2_baseline, timeout_seconds)
            actual_send_count = 1
            worker_observations = observer.finish()
            observer = None
            db_evidence = run_database_async(
                _collect_database_evidence(message_id, fetch_job_id, plan["expected"], worker_observations)
            )
            report["evidence"]["outbound_imap"] = _validate_outbound_mail(outbound[0], message_id, db_evidence["rma"]["pdf_sha256"])
            report["evidence"]["database"] = db_evidence
            report["status"] = "passed_pending_cleanup"
        except Exception as exc:
            primary_error = exc
            report["status"] = "failed" if pre_relay_validated else "awaiting_confirmation"
            report["error"] = {"code": getattr(exc, "code", type(exc).__name__), "details": getattr(exc, "details", {})}
            report["continuation_blocked"] = not pre_relay_validated
        finally:
            if observer is not None:
                try:
                    report["evidence"]["worker_observations"] = observer.finish()
                except Exception as exc:
                    report["evidence"]["worker_observer_error"] = getattr(exc, "code", type(exc).__name__)
            report["actual_send_count"] = actual_send_count
            cleanup_errors: list[dict[str, str]] = []
            database_cleanup_safe = False
            if initial is not None:
                try:
                    report["restored_config"] = _restore_config(client, initial)
                except Exception as exc:
                    cleanup_errors.append({"step": "restore_config", "error": getattr(exc, "code", type(exc).__name__)})
            if user_id:
                try:
                    cleanup_db = run_database_async(_cleanup_database(str(plan["inbound"]["message_id"]), confirm_run_id, user_id, timeout_seconds))
                    report["cleanup"] = {"status": "database_complete", "database": cleanup_db}
                    if cleanup_db.get("status") in {"blocked", "cleanup_pending"} or not (cleanup_db.get("verification") or {}).get("verified"):
                        raise LiveChainError("DATABASE_OR_OSS_CLEANUP_INCOMPLETE")
                    database_cleanup_safe = True
                except Exception as exc:
                    cleanup_errors.append({"step": "database_and_oss", "error": getattr(exc, "code", type(exc).__name__)})
            try:
                if state_path.exists() and database_cleanup_safe:
                    run_database_async(cleanup_temporary_master_data(master_manifest_path, state_path=state_path, skip_manifest_validation=True))
                elif state_path.exists():
                    raise LiveChainError("TEMPORARY_MASTER_CLEANUP_BLOCKED_BY_DATABASE")
            except Exception as exc:
                cleanup_errors.append({"step": "temporary_master_data", "error": getattr(exc, "code", type(exc).__name__)})
            try:
                _relay_reset()
            except Exception as exc:
                cleanup_errors.append({"step": "relay_reset", "error": getattr(exc, "code", type(exc).__name__)})
            report["cleanup"]["errors"] = cleanup_errors
            report["cleanup"]["mailboxes_preserved"] = True
            if cleanup_errors:
                report["cleanup"]["status"] = "incomplete"
                report["status"] = "failed"
            elif report["status"] == "passed_pending_cleanup":
                report["cleanup"]["status"] = "complete"
                report["status"] = "passed"
            report["finished_at"] = now_iso()
            _write_json(result_path, report)
    if primary_error is not None or report["status"] != "passed":
        raise LiveChainError("LIVE_BUSINESS_CHAIN_FAILED", details={"result": str(result_path), "status": report["status"]})
    return report


def create_plan(run_id: str) -> dict[str, Any]:
    root = _run_root(run_id)
    path = root / "plan.json"
    if root.exists():
        raise LiveChainError("RUN_ID_ALREADY_EXISTS")
    plan = build_plan(run_id)
    _write_json(path, plan)
    return {"status": "planned", "run_id": run_id, "plan_path": str(path), "plan_hash": plan["plan_hash"], "expected_smtp_send_count": EXPECTED_SEND_COUNT, "messages_sent": 0}


def report_run(run_id: str) -> dict[str, Any]:
    path = _run_root(run_id) / "result.json"
    if not path.exists():
        raise LiveChainError("RUN_REPORT_NOT_FOUND")
    return _read_json(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="受控真实完整业务链路测试")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser("doctor", help="零发送检查")
    doctor_parser.add_argument("--live", action="store_true", help="登录真实邮箱和外部服务，但不发送")
    plan_parser = sub.add_parser("plan", help="生成哈希绑定计划")
    plan_parser.add_argument("--run-id", required=True)
    run_parser = sub.add_parser("run", help="固定历史 IMAP 邮件重放；最多发送一封真实回复")
    run_parser.add_argument("--plan", type=Path, required=True)
    run_parser.add_argument("--plan-hash", required=True)
    run_parser.add_argument("--confirm-run-id", required=True)
    run_parser.add_argument("--approved-by", required=True)
    run_parser.add_argument("--i-understand-real-mail", action="store_true")
    run_parser.add_argument("--i-authorize-ai-oss-egress", action="store_true")
    run_parser.add_argument("--timeout-seconds", type=int, default=300)
    report_parser = sub.add_parser("report", help="读取脱敏结果")
    report_parser.add_argument("--run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        value = doctor(args.live)
    elif args.command == "plan":
        value = create_plan(args.run_id)
    elif args.command == "report":
        value = report_run(args.run_id)
    else:
        if not args.i_understand_real_mail or not args.i_authorize_ai_oss_egress:
            raise LiveChainError("EXPLICIT_REAL_MAIL_AND_EGRESS_APPROVAL_REQUIRED")
        if not 30 <= args.timeout_seconds <= 1800:
            raise LiveChainError("TIMEOUT_OUT_OF_RANGE")
        value = execute_run(args.plan, args.plan_hash, args.confirm_run_id, args.approved_by, args.timeout_seconds)
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    return 0 if value.get("status") in {"passed", "planned"} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LiveChainError as exc:
        print(json.dumps({"status": "failed", "error": {"code": exc.code, "details": exc.details}}, ensure_ascii=False, indent=2))
        raise SystemExit(1) from exc
    finally:
        try:
            asyncio.run(engine.dispose())
        except RuntimeError:
            pass
