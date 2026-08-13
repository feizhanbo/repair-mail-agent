from __future__ import annotations

import argparse
import asyncio
import hashlib
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, make_msgid
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.core.database import AsyncSessionLocal
from app.models import User, WorkflowExecution, WorkflowInterrupt
from app.services.gold_replay import (
    GoldReplayError,
    apply_gold_test_reset,
    assert_gold_replay_environment,
    plan_gold_test_reset,
    verify_gold_test_reset,
)
from app.services.mail_safety import TEST_MAIL_RECIPIENT, TEST_MAIL_SENDER, test_mail_configuration_reasons
from tools.run_new_repair_mail_e2e import Client, current_config, find_email, login, patch_config, wait_for_job
from tools.run_rmatest_batch_e2e import apply_temporary_master_data, cleanup_temporary_master_data


SCHEMA_VERSION = 3
MESSAGE_ID_PATTERN = re.compile(r"^<[^<>\s]+>$")
RMA_PATTERN = re.compile(r"^\d{10}$")
INTENTS = {
    "new_repair", "customer_supplement", "normal_reply", "rma_sent",
    "device_received", "irrelevant", "unknown",
}
SEND_MODES = {"none", "auto_rma", "auto_followup", "followup_then_rma"}
TERMINAL_PARSE_STATUSES = {"parsed", "failed", "manual_review", "irrelevant"}
PROJECT_ROOT = BACKEND_ROOT.parent
EVIDENCE_ROOT = PROJECT_ROOT / "test-results" / "gold-mail-regression"
ARCHIVE_DOC = PROJECT_ROOT / "docs" / "12-rmatest金标邮件可重复全链路回归测试记录.md"


class GoldCliError(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_out(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldCliError("JSON_READ_FAILED", details={"path": str(path), "error": str(exc)}) from exc
    if not isinstance(value, dict):
        raise GoldCliError("JSON_OBJECT_REQUIRED", details={"path": str(path)})
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def suite_root(suite_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,80}", suite_id):
        raise GoldCliError("SUITE_ID_INVALID")
    return EVIDENCE_ROOT / suite_id


def normalized_message_id(value: str) -> str:
    result = str(value or "").strip()
    if not result.startswith("<"):
        result = f"<{result}>"
    if not MESSAGE_ID_PATTERN.fullmatch(result):
        raise GoldCliError("MESSAGE_ID_INVALID", details={"message_id_sha256": hashlib.sha256(result.encode()).hexdigest()})
    return result


def _imap_connect(*, host: str, port: int, user: str, password: str, use_ssl: bool = True) -> imaplib.IMAP4:
    if not host or not user or not password:
        raise GoldCliError("IMAP_CONFIGURATION_INCOMPLETE", details={"user_configured": bool(user), "host_configured": bool(host)})
    client: imaplib.IMAP4
    if use_ssl:
        client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
    else:
        client = imaplib.IMAP4(host, port, timeout=30)
        client.starttls(ssl_context=ssl.create_default_context())
    client.login(user, password)
    return client


def _fetch_raw_by_message_id(
    *, host: str, port: int, user: str, password: str, folder: str, message_id: str, use_ssl: bool = True,
) -> tuple[str, bytes, str | None]:
    client = _imap_connect(host=host, port=port, user=user, password=password, use_ssl=use_ssl)
    try:
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            raise GoldCliError("IMAP_FOLDER_SELECT_FAILED")
        uid_validity_response = client.response("UIDVALIDITY")
        uid_validity = uid_validity_response[1][0].decode("ascii", errors="replace") if uid_validity_response[1] else None
        status, data = client.uid("search", None, "HEADER", "Message-ID", message_id)
        uids = (data[0] or b"").split() if status == "OK" else []
        if len(uids) != 1:
            raise GoldCliError("IMAP_MESSAGE_ID_MATCH_COUNT_INVALID", details={"match_count": len(uids)})
        status, fetched = client.uid("fetch", uids[0], "(BODY.PEEK[])")
        raw = next((part[1] for part in fetched or [] if isinstance(part, tuple) and isinstance(part[1], bytes)), b"")
        if status != "OK" or not raw:
            raise GoldCliError("IMAP_MESSAGE_FETCH_FAILED")
        return uids[0].decode("ascii"), raw, uid_validity
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _message_metadata(uid: str, raw: bytes, uid_validity: str | None) -> dict[str, Any]:
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    attachments: list[dict[str, Any]] = []
    for part in parsed.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if filename or disposition == "attachment":
            content = part.get_payload(decode=True) or b""
            attachments.append({
                "filename_sha256": hashlib.sha256(str(filename or "").encode("utf-8")).hexdigest(),
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "content_type": part.get_content_type(),
                "size_bytes": len(content),
            })
    return {
        "uid": uid,
        "uid_validity": uid_validity,
        "message_id": normalized_message_id(str(parsed.get("Message-ID") or "")),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "header": {
            "subject": str(parsed.get("Subject") or ""),
            "from": str(parsed.get("From") or ""),
            "to": str(parsed.get("To") or ""),
            "cc": str(parsed.get("Cc") or ""),
            "date": str(parsed.get("Date") or ""),
        },
        "attachments": attachments,
    }


def inventory(suite_id: str, message_ids: list[str]) -> dict[str, Any]:
    unique = list(dict.fromkeys(normalized_message_id(value) for value in message_ids))
    if not unique:
        raise GoldCliError("MESSAGE_IDS_REQUIRED")
    messages: list[dict[str, Any]] = []
    for message_id in unique:
        uid, raw, uid_validity = _fetch_raw_by_message_id(
            host=settings.IMAP_HOST, port=settings.IMAP_PORT, user=settings.IMAP_USER,
            password=settings.IMAP_PASSWORD, folder=settings.IMAP_FOLDER,
            message_id=message_id, use_ssl=True,
        )
        metadata = _message_metadata(uid, raw, uid_validity)
        metadata["gold"] = {
            "expected_intent": None,
            "expected_subtype": None,
            "expected_fields": {},
            "expected_items": [],
            "missing_fields": [],
            "create_ticket": None,
            "expected_final_status": None,
            "expected_outbound_count": 0,
            "send_mode": "none",
            "fixed_rma_no": None,
            "temporary_sn_assets": [],
            "temporary_board_cards": [],
            "temporary_customer_policies": [],
            "supplement": None,
        }
        messages.append(metadata)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite_id,
        "created_at": now_iso(),
        "source_mailbox": TEST_MAIL_SENDER,
        "outbound_recipient_only": TEST_MAIL_RECIPIENT,
        "max_actual_sends": 0,
        "messages": messages,
    }
    path = suite_root(suite_id) / "manifest.json"
    write_json(path, manifest)
    return {"status": "inventory_created", "manifest": str(path), "message_count": len(messages), "messages_sent": 0}


def validate_manifest(path: Path, *, require_approval: bool = False) -> dict[str, Any]:
    manifest = read_json(path)
    errors: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("SCHEMA_VERSION_MUST_EQUAL_3")
    if manifest.get("source_mailbox", "").lower() != TEST_MAIL_SENDER:
        errors.append("SOURCE_MAILBOX_MUST_BE_RMATEST1")
    if manifest.get("outbound_recipient_only", "").lower() != TEST_MAIL_RECIPIENT:
        errors.append("OUTBOUND_RECIPIENT_MUST_BE_RMATEST2")
    messages = manifest.get("messages")
    if not isinstance(messages, list) or not messages:
        errors.append("MESSAGES_REQUIRED")
        messages = []
    seen: set[str] = set()
    planned_sends = 0
    for index, item in enumerate(messages):
        prefix = f"messages[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}_MUST_BE_OBJECT")
            continue
        message_id = str(item.get("message_id") or "")
        if not MESSAGE_ID_PATTERN.fullmatch(message_id):
            errors.append(f"{prefix}.message_id_INVALID")
        if message_id in seen:
            errors.append(f"{prefix}.message_id_DUPLICATE")
        seen.add(message_id)
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("raw_sha256") or "")):
            errors.append(f"{prefix}.raw_sha256_INVALID")
        gold = item.get("gold") if isinstance(item.get("gold"), dict) else {}
        intent = gold.get("expected_intent")
        subtype = gold.get("expected_subtype")
        if intent not in INTENTS:
            errors.append(f"{prefix}.expected_intent_INVALID")
        if intent == "irrelevant" and subtype not in {"general_irrelevant", "out_of_scope_repair"}:
            errors.append(f"{prefix}.expected_subtype_REQUIRED")
        if intent != "irrelevant" and subtype is not None:
            errors.append(f"{prefix}.expected_subtype_MUST_BE_NULL")
        if not isinstance(gold.get("create_ticket"), bool):
            errors.append(f"{prefix}.create_ticket_REQUIRED")
        if gold.get("send_mode") not in SEND_MODES:
            errors.append(f"{prefix}.send_mode_INVALID")
        count = gold.get("expected_outbound_count")
        if not isinstance(count, int) or count < 0:
            errors.append(f"{prefix}.expected_outbound_count_INVALID")
            count = 0
        planned_sends += count
        if gold.get("send_mode") == "none" and count:
            errors.append(f"{prefix}.SEND_MODE_NONE_WITH_OUTBOUND")
        if gold.get("send_mode") in {"auto_rma", "followup_then_rma"}:
            rma_no = str(gold.get("fixed_rma_no") or "")
            try:
                valid_rma = bool(RMA_PATTERN.fullmatch(rma_no)) and datetime.strptime(rma_no[:8], "%Y%m%d").strftime("%Y%m%d") == rma_no[:8] and rma_no[-2:] != "00"
            except ValueError:
                valid_rma = False
            if not valid_rma:
                errors.append(f"{prefix}.fixed_rma_no_INVALID")
        if gold.get("send_mode") == "followup_then_rma" and not isinstance(gold.get("supplement"), dict):
            errors.append(f"{prefix}.supplement_REQUIRED")
        for key, expected_type in (("expected_fields", dict), ("expected_items", list), ("missing_fields", list)):
            if not isinstance(gold.get(key), expected_type):
                errors.append(f"{prefix}.{key}_INVALID")
        if gold.get("create_ticket") and not gold.get("expected_final_status"):
            errors.append(f"{prefix}.expected_final_status_REQUIRED")
    if manifest.get("max_actual_sends") != planned_sends:
        errors.append("MAX_ACTUAL_SENDS_MUST_EQUAL_PLANNED_SENDS")
    approval_path = path.parent / "approval.json"
    approved = False
    if approval_path.exists():
        approval = read_json(approval_path)
        approved = approval.get("manifest_sha256") == file_sha256(path) and bool(approval.get("approved_by"))
    if require_approval and not approved:
        errors.append("UNCHANGED_MANIFEST_APPROVAL_REQUIRED")
    result = {"valid": not errors, "errors": errors, "message_count": len(messages), "planned_sends": planned_sends, "approved": approved}
    if errors:
        raise GoldCliError("MANIFEST_INVALID", details=result)
    return result


def approve_manifest(path: Path, approved_by: str, acknowledge: bool) -> dict[str, Any]:
    validate_manifest(path)
    if not acknowledge:
        raise GoldCliError("REAL_MAIL_ACKNOWLEDGEMENT_REQUIRED")
    approval = {"schema_version": 1, "manifest_sha256": file_sha256(path), "approved_by": approved_by, "approved_at": now_iso()}
    target = path.parent / "approval.json"
    write_json(target, approval)
    return {"status": "approved", "approval": str(target), "manifest_sha256": approval["manifest_sha256"]}


def _check(name: str, passed: bool, code: str, detail: Any = None) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "code": "OK" if passed else code, "detail": detail}


async def _database_doctor() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        try:
            snapshot = await assert_gold_replay_environment(session)
            return {"passed": True, "detail": snapshot}
        except GoldReplayError as exc:
            return {"passed": False, "detail": {"code": exc.code, **exc.details}}
        except Exception as exc:
            return {
                "passed": False,
                "detail": {"code": "DATABASE_CONNECTION_FAILED", "exception_type": type(exc).__name__},
            }


def doctor(*, live: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    mail_reasons = test_mail_configuration_reasons()
    checks.append(_check("rmatest1_mail_gate", not mail_reasons, "RMATEST1_MAIL_GATE_FAILED", mail_reasons))
    checks.append(_check("rmatest2_identity", settings.E2E_RMATEST2_IMAP_USER.lower() == TEST_MAIL_RECIPIENT and settings.E2E_RMATEST2_SMTP_USER.lower() == TEST_MAIL_RECIPIENT, "RMATEST2_IDENTITY_INVALID"))
    checks.append(_check("rmatest2_hosts", settings.E2E_RMATEST2_IMAP_HOST == "imaphz.qiye.163.com" and settings.E2E_RMATEST2_IMAP_PORT == 993 and settings.E2E_RMATEST2_SMTP_HOST == "smtphz.qiye.163.com" and settings.E2E_RMATEST2_SMTP_PORT == 465, "RMATEST2_ENDPOINT_INVALID"))
    checks.append(_check("rmatest2_credentials_present", bool(settings.E2E_RMATEST2_IMAP_PASSWORD and settings.E2E_RMATEST2_SMTP_PASSWORD), "RMATEST2_CREDENTIALS_MISSING"))
    checks.append(_check("real_mail_gate", settings.RUN_REAL_MAIL_INTEGRATION_TESTS and settings.E2E_GOLD_RUN_ENABLED, "REAL_MAIL_GATES_DISABLED"))
    db = asyncio.run(_database_doctor())
    checks.append(_check("database_and_relay_gate", db["passed"], "DATABASE_OR_RELAY_GATE_FAILED", db["detail"]))
    if live:
        for label, config in (
            ("rmatest1_imap_live", (settings.IMAP_HOST, settings.IMAP_PORT, settings.IMAP_USER, settings.IMAP_PASSWORD, True)),
            ("rmatest2_imap_live", (settings.E2E_RMATEST2_IMAP_HOST, settings.E2E_RMATEST2_IMAP_PORT, settings.E2E_RMATEST2_IMAP_USER, settings.E2E_RMATEST2_IMAP_PASSWORD, settings.E2E_RMATEST2_IMAP_USE_SSL)),
        ):
            try:
                client = _imap_connect(host=config[0], port=config[1], user=config[2], password=config[3], use_ssl=config[4])
                client.noop()
                client.logout()
                checks.append(_check(label, True, ""))
            except Exception as exc:
                checks.append(_check(label, False, "IMAP_LIVE_CHECK_FAILED", type(exc).__name__))
        try:
            with smtplib.SMTP_SSL(settings.E2E_RMATEST2_SMTP_HOST, settings.E2E_RMATEST2_SMTP_PORT, context=ssl.create_default_context(), timeout=30) as smtp:
                smtp.login(settings.E2E_RMATEST2_SMTP_USER, settings.E2E_RMATEST2_SMTP_PASSWORD)
                smtp.noop()
            checks.append(_check("rmatest2_smtp_live", True, ""))
        except Exception as exc:
            checks.append(_check("rmatest2_smtp_live", False, "SMTP_LIVE_CHECK_FAILED", type(exc).__name__))
    return {"status": "passed" if all(row["passed"] for row in checks) else "blocked", "checks": checks, "secrets_exposed": False, "live_checks": live}


def _relay_control(scenario: str, rma_no: str | None = None) -> None:
    if not settings.TEST_RELAY_BASE_URL or not settings.TEST_RELAY_TOKEN:
        raise GoldCliError("TEST_RELAY_CONFIGURATION_MISSING")
    body = json.dumps({"scenario": scenario, "delay_seconds": 0, "rma_no": rma_no}).encode("utf-8")
    request = Request(f"{settings.TEST_RELAY_BASE_URL.rstrip('/')}/control/default", method="PUT", data=body, headers={"Authorization": f"Bearer {settings.TEST_RELAY_TOKEN}", "Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise GoldCliError("TEST_RELAY_CONTROL_FAILED")


def _relay_reset() -> None:
    request = Request(f"{settings.TEST_RELAY_BASE_URL.rstrip('/')}/control/reset", method="POST", headers={"Authorization": f"Bearer {settings.TEST_RELAY_TOKEN}"})
    with urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise GoldCliError("TEST_RELAY_RESET_FAILED")


async def _reset(message_ids: list[str], *, suite_id: str, run_id: str, apply: bool, expected_hash: str | None = None) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        await assert_gold_replay_environment(session)
        plan = await plan_gold_test_reset(session, message_ids=message_ids)
        if not apply:
            return plan
        if expected_hash and expected_hash != plan["plan_hash"]:
            raise GoldCliError("CLEANUP_PLAN_HASH_MISMATCH", details={"actual_plan_hash": plan["plan_hash"]})
        if plan["blockers"]:
            raise GoldCliError("CLEANUP_BLOCKED", details={"blockers": plan["blockers"]})
        user_id = await session.scalar(
            select(User.id).where(User.username == settings.DEFAULT_ADMIN_USERNAME)
        )
        if not user_id:
            raise GoldCliError("DEFAULT_ADMIN_USER_NOT_FOUND")
        graph_thread_ids = list(plan["resource_ids"].get("graph_threads") or [])
        result = await apply_gold_test_reset(
            session, message_ids=message_ids, expected_plan_hash=plan["plan_hash"],
            suite_id=suite_id, run_id=run_id, user_id=int(user_id),
            reason=f"Approved gold regression replay cleanup for {suite_id}/{run_id}",
        )
        result["verification"] = await verify_gold_test_reset(
            session,
            message_ids=message_ids,
            graph_thread_ids=graph_thread_ids,
        )
        result["verification"]["checkpoint_thread_count"] = int(
            result.get("checkpoint_thread_count") or 0
        )
        result["verification"]["verified"] = (
            result["verification"]["verified"]
            and result["verification"]["checkpoint_thread_count"] == 0
        )
        return result


def cleanup(path: Path, *, apply: bool, expected_hash: str | None) -> dict[str, Any]:
    manifest = read_json(path)
    message_ids = [str(item["message_id"]) for item in manifest.get("messages") or []]
    result = asyncio.run(_reset(message_ids, suite_id=str(manifest["suite_id"]), run_id=f"cleanup-{datetime.now().strftime('%Y%m%d%H%M%S')}", apply=apply, expected_hash=expected_hash))
    temporary_candidates = sorted(
        suite_root(str(manifest["suite_id"])).glob(
            "runs/*/case-*/case-manifest.json"
        )
    )
    temporary_results: list[dict[str, Any]] = []
    if apply:
        for candidate in temporary_candidates:
            state_path = candidate.parent / "temporary-master-state.json"
            if not state_path.exists():
                continue
            temporary_results.append(
                {
                    "case": str(candidate.parent.relative_to(suite_root(str(manifest["suite_id"])))),
                    "result": asyncio.run(
                        cleanup_temporary_master_data(
                            candidate,
                            state_path=state_path,
                            skip_manifest_validation=True,
                        )
                    ),
                }
            )
        _relay_reset()
    return {
        "status": "applied" if apply else "preview",
        "result": result,
        "temporary_master_state_files": len(temporary_candidates),
        "temporary_master_cleanup": temporary_results,
        "relay_reset": bool(apply),
    }


def _fetch_system_message(client: Client, message_id: str) -> tuple[int | None, dict[str, Any]]:
    response = client.data("POST", "/api/v1/emails/fetch/jobs", params={"folder_name": "INBOX", "limit": 1, "unseen_only": "false", "message_id": message_id, "auto_parse": "true"})
    job_payload = response.get("job") if isinstance(response, dict) else None
    if not isinstance(job_payload, dict) or not job_payload.get("id") or response.get("reused"):
        raise GoldCliError("IMAP_FETCH_JOB_NOT_CREATED")
    job = wait_for_job(client, int(job_payload["id"]))
    fetched = (job.get("result_json") or {}).get("fetched") or []
    match = [row for row in fetched if row.get("message_id") == message_id]
    if len(match) != 1:
        raise GoldCliError("IMAP_EXACT_FETCH_RESULT_INVALID", details={"match_count": len(match)})
    email_id = match[0].get("email_id")
    if not email_id:
        found = find_email(client, message_id)
        email_id = found.get("id") if found else None
    return int(email_id) if email_id else None, match[0]


def _wait_for_case(client: Client, email_id: int, expected_status: str, expected_outbound: int, timeout_seconds: int = 300) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        email_detail = client.data("GET", f"/api/v1/emails/{email_id}")
        ticket_ids = [row.get("ticket_id") for row in email_detail.get("parse_results") or [] if row.get("ticket_id")]
        if ticket_ids:
            ticket_detail = client.data("GET", f"/api/v1/tickets/{int(ticket_ids[0])}")
            ticket = ticket_detail.get("ticket") or {}
            sent = [row for row in ticket_detail.get("reply_records") or [] if row.get("send_status") == "sent"]
            last = {"email_detail": email_detail, "ticket_detail": ticket_detail}
            if ticket.get("current_status_code") == expected_status and len(sent) >= expected_outbound:
                return last
        else:
            last = {"email_detail": email_detail, "ticket_detail": None}
            email = email_detail.get("email") or {}
            if not expected_status and email.get("parse_status") in TERMINAL_PARSE_STATUSES:
                return last
        time.sleep(2)
    raise GoldCliError("CASE_TIMEOUT", details={"last": last})


async def _graph_execution_evidence(email_id: int) -> dict[str, Any]:
    """Collect sanitized ownership/recovery evidence for a gold-mail case."""
    async with AsyncSessionLocal() as session:
        execution = await session.scalar(
            select(WorkflowExecution)
            .where(
                WorkflowExecution.email_id == email_id,
                WorkflowExecution.workflow_name == "email_ticket",
            )
            .order_by(WorkflowExecution.id.desc())
        )
        if execution is None:
            return {"found": False, "interrupts": []}
        rows = (
            await session.execute(
                select(WorkflowInterrupt)
                .where(WorkflowInterrupt.execution_id == execution.execution_id)
                .order_by(WorkflowInterrupt.id)
            )
        ).scalars().all()
        return {
            "found": True,
            "execution_id_sha256": hashlib.sha256(execution.execution_id.encode()).hexdigest(),
            "workflow_version": execution.workflow_version,
            "state_schema_version": execution.state_schema_version,
            "execution_mode": execution.execution_mode,
            "status": execution.status,
            "checkpoint_present": bool(execution.checkpoint_id),
            "checkpoint_step": execution.checkpoint_step,
            "last_error_code": execution.last_error_code,
            "interrupts": [
                {
                    "status": row.status,
                    "manual": row.manual_task_id is not None,
                    "checkpoint_present": bool(row.checkpoint_id),
                    "checkpoint_step": row.checkpoint_step,
                    "error_present": bool(row.error_message),
                }
                for row in rows
            ],
        }


def _assert_graph_execution_evidence(evidence: dict[str, Any]) -> list[str]:
    if settings.WORKFLOW_ENGINE != "langgraph":
        return []
    issues: list[str] = []
    if not evidence.get("found"):
        return ["LANGGRAPH_EXECUTION_MISSING"]
    if evidence.get("workflow_version") != "langgraph-v2":
        issues.append("LANGGRAPH_WORKFLOW_VERSION_MISMATCH")
    if evidence.get("execution_mode") != "langgraph":
        issues.append("LANGGRAPH_EXECUTION_MODE_MISMATCH")
    if not evidence.get("checkpoint_present") or evidence.get("checkpoint_step") is None:
        issues.append("LANGGRAPH_EXECUTION_CHECKPOINT_MISSING")
    if evidence.get("status") not in {"completed", "waiting_human", "waiting_external"}:
        issues.append("LANGGRAPH_EXECUTION_NOT_STABLE")
    if evidence.get("last_error_code"):
        issues.append("LANGGRAPH_EXECUTION_HAS_ERROR")
    for row in evidence.get("interrupts") or []:
        if not row.get("checkpoint_present") or row.get("checkpoint_step") is None:
            issues.append("LANGGRAPH_INTERRUPT_CHECKPOINT_MISSING")
        if row.get("status") in {"pending", "resume_queued"} and row.get("error_present"):
            issues.append("LANGGRAPH_INTERRUPT_HAS_ERROR")
    return sorted(set(issues))


def _rmatest2_max_uid() -> int:
    client = _imap_connect(host=settings.E2E_RMATEST2_IMAP_HOST, port=settings.E2E_RMATEST2_IMAP_PORT, user=settings.E2E_RMATEST2_IMAP_USER, password=settings.E2E_RMATEST2_IMAP_PASSWORD, use_ssl=settings.E2E_RMATEST2_IMAP_USE_SSL)
    try:
        client.select(settings.E2E_RMATEST2_IMAP_FOLDER, readonly=True)
        status, data = client.uid("search", None, "ALL")
        uids = [int(value) for value in (data[0] or b"").split()] if status == "OK" else []
        return max(uids, default=0)
    finally:
        client.logout()


def _rmatest2_new_messages(after_uid: int) -> list[dict[str, Any]]:
    client = _imap_connect(host=settings.E2E_RMATEST2_IMAP_HOST, port=settings.E2E_RMATEST2_IMAP_PORT, user=settings.E2E_RMATEST2_IMAP_USER, password=settings.E2E_RMATEST2_IMAP_PASSWORD, use_ssl=settings.E2E_RMATEST2_IMAP_USE_SSL)
    try:
        client.select(settings.E2E_RMATEST2_IMAP_FOLDER, readonly=True)
        status, data = client.uid("search", None, "UID", f"{after_uid + 1}:*")
        result: list[dict[str, Any]] = []
        for uid in (data[0] or b"").split() if status == "OK" else []:
            if int(uid) <= after_uid:
                continue
            status, fetched = client.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((part[1] for part in fetched or [] if isinstance(part, tuple) and isinstance(part[1], bytes)), b"")
            if status != "OK" or not raw:
                continue
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            attachments = [{"filename": part.get_filename(), "content_type": part.get_content_type(), "size_bytes": len(part.get_payload(decode=True) or b""), "sha256": hashlib.sha256(part.get_payload(decode=True) or b"").hexdigest()} for part in msg.iter_attachments()]
            result.append({"uid": int(uid), "message_id": str(msg.get("Message-ID") or ""), "subject": str(msg.get("Subject") or ""), "from": str(msg.get("From") or ""), "to": str(msg.get("To") or ""), "cc": str(msg.get("Cc") or ""), "in_reply_to": str(msg.get("In-Reply-To") or ""), "references": str(msg.get("References") or ""), "attachments": attachments})
        return result
    finally:
        client.logout()


def _send_supplement(original_message_id: str, reply_message: dict[str, Any], supplement: dict[str, Any]) -> str:
    message_id = make_msgid(domain="accotest.com")
    msg = EmailMessage()
    msg["From"] = TEST_MAIL_RECIPIENT
    msg["To"] = TEST_MAIL_SENDER
    msg["Subject"] = str(supplement.get("subject") or f"Re: {reply_message.get('subject') or ''}")
    msg["Message-ID"] = message_id
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["In-Reply-To"] = str(reply_message.get("message_id") or original_message_id)
    msg["References"] = " ".join(dict.fromkeys([original_message_id, str(reply_message.get("message_id") or "")]))
    msg.set_content(str(supplement.get("body_text") or ""))
    if not str(supplement.get("body_text") or "").strip():
        raise GoldCliError("SUPPLEMENT_BODY_REQUIRED")
    with smtplib.SMTP_SSL(settings.E2E_RMATEST2_SMTP_HOST, settings.E2E_RMATEST2_SMTP_PORT, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(settings.E2E_RMATEST2_SMTP_USER, settings.E2E_RMATEST2_SMTP_PASSWORD)
        smtp.send_message(msg, from_addr=TEST_MAIL_RECIPIENT, to_addrs=[TEST_MAIL_SENDER])
    return message_id


def _assert_case(item: dict[str, Any], value: dict[str, Any], outbound: list[dict[str, Any]]) -> list[str]:
    gold = item["gold"]
    issues: list[str] = []
    email = (value.get("email_detail") or {}).get("email") or {}
    ticket_detail = value.get("ticket_detail") or {}
    ticket = ticket_detail.get("ticket") or {}
    if email.get("intent_type") != gold.get("expected_intent"):
        issues.append("INTENT_MISMATCH")
    if email.get("intent_subtype") != gold.get("expected_subtype"):
        issues.append("INTENT_SUBTYPE_MISMATCH")
    if bool(ticket) != bool(gold.get("create_ticket")):
        issues.append("TICKET_CREATION_MISMATCH")
    if ticket and ticket.get("current_status_code") != gold.get("expected_final_status"):
        issues.append("FINAL_STATUS_MISMATCH")
    for key, expected in (gold.get("expected_fields") or {}).items():
        if ticket.get(key) != expected:
            issues.append(f"FIELD_MISMATCH:{key}")
    if ticket:
        actual_missing = sorted((ticket.get("missing_fields") or {}).keys())
        if actual_missing != sorted(gold.get("missing_fields") or []):
            issues.append("MISSING_FIELDS_MISMATCH")
        actual_items = ticket_detail.get("items") or []
        for expected_item in gold.get("expected_items") or []:
            if not any(all(row.get(key) == expected for key, expected in expected_item.items()) for row in actual_items):
                issues.append("TICKET_ITEM_MISMATCH")
        fixed_rma = gold.get("fixed_rma_no")
        if fixed_rma and fixed_rma not in {row.get("rma_no") for row in ticket_detail.get("rma_records") or []}:
            issues.append("FIXED_RMA_MISMATCH")
        sent_replies = [
            row
            for row in ticket_detail.get("reply_records") or []
            if row.get("send_status") == "sent"
        ]
        if any(not row.get("template_id") for row in sent_replies):
            issues.append("SENT_REPLY_WITHOUT_TEMPLATE")
        sap_rows = ticket_detail.get("sap_export_items") or ticket_detail.get("sap_export_records") or []
        keys = [row.get("submission_key") for row in sap_rows if row.get("submission_key")]
        if len(keys) != len(set(keys)):
            issues.append("SUBMISSION_KEY_DUPLICATE")
    matching = [row for row in outbound if row.get("in_reply_to") == item["message_id"] or item["message_id"] in row.get("references", "")]
    if len(matching) != int(gold.get("expected_outbound_count") or 0):
        issues.append("OUTBOUND_COUNT_MISMATCH")
    for row in matching:
        recipients = [address.lower() for _, address in getaddresses([row.get("to") or ""]) if address]
        cc = [address for _, address in getaddresses([row.get("cc") or ""]) if address]
        if recipients != [TEST_MAIL_RECIPIENT] or cc:
            issues.append("OUTBOUND_ENVELOPE_INVALID")
        if not str(row.get("subject") or "").upper().startswith("[TEST ONLY]"):
            issues.append("OUTBOUND_TEST_SUBJECT_MISSING")
    fixed_rma = str(gold.get("fixed_rma_no") or "")
    if fixed_rma:
        rma_messages = [
            row
            for row in matching
            if fixed_rma in str(row.get("subject") or "")
        ]
        if len(rma_messages) != 1:
            issues.append("RMA_OUTBOUND_NOT_UNIQUE")
        else:
            pdfs = [
                attachment
                for attachment in rma_messages[0].get("attachments") or []
                if attachment.get("content_type") == "application/pdf"
                or str(attachment.get("filename") or "").lower().endswith(".pdf")
            ]
            if len(pdfs) != 1:
                issues.append("RMA_PDF_ATTACHMENT_COUNT_INVALID")
            else:
                filename = str(pdfs[0].get("filename") or "")
                normalized_subject = re.sub(
                    r"^\[TEST ONLY\]\s*",
                    "",
                    str(rma_messages[0].get("subject") or ""),
                    flags=re.IGNORECASE,
                ).strip()
                if fixed_rma not in filename:
                    issues.append("RMA_PDF_FILENAME_NUMBER_MISMATCH")
                if Path(filename).stem != normalized_subject:
                    issues.append("RMA_SUBJECT_FILENAME_MISMATCH")
                if int(pdfs[0].get("size_bytes") or 0) > 5 * 1024 * 1024:
                    issues.append("RMA_PDF_SIZE_EXCEEDS_5MB")
    return sorted(set(issues))


def _compare_with_previous(run_root: Path, result: dict[str, Any]) -> dict[str, Any]:
    prior_paths = sorted(run_root.parent.glob("*/result.json"))
    prior_paths = [path for path in prior_paths if path.parent != run_root]
    if not prior_paths:
        return {"available": False, "changes": []}
    previous = read_json(prior_paths[-1])
    previous_by_hash = {
        row.get("message_id_sha256"): row for row in previous.get("cases") or []
    }
    changes: list[dict[str, Any]] = []
    for current in result.get("cases") or []:
        before = previous_by_hash.get(current.get("message_id_sha256"))
        if before is None:
            changes.append({"message_id_sha256": current.get("message_id_sha256"), "change": "new_case"})
            continue
        fields = {}
        for key in ("status", "actual_intent", "actual_status", "issues"):
            if before.get(key) != current.get(key):
                fields[key] = {"before": before.get(key), "after": current.get(key)}
        if fields:
            changes.append({"message_id_sha256": current.get("message_id_sha256"), "change": "result_changed", "fields": fields})
    return {"available": True, "previous_run_id": previous.get("run_id"), "changes": changes}


def _mini_manifest(path: Path, manifest: dict[str, Any], item: dict[str, Any]) -> Path:
    target = path / "case-manifest.json"
    write_json(target, {"schema_version": 2, "batch_id": manifest["suite_id"], "messages": [item]})
    return target


def _append_archive(run: dict[str, Any]) -> None:
    if not ARCHIVE_DOC.exists():
        ARCHIVE_DOC.write_text("# rmatest 金标邮件可重复全链路回归测试记录\n\n此文档只保存脱敏结果；原始邮件正文与附件不写入文档。\n", encoding="utf-8")
    lines = [
        "", f"## {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')} — `{run['suite_id']}` / `{run['run_id']}`", "",
        f"- 结果：`{run['status']}`", f"- 用例：`{len(run.get('cases') or [])}`", f"- 实际出站：`{run.get('actual_outbound_count', 0)}`", f"- 清理：`{run.get('cleanup_status', 'unknown')}`", "",
        "| Message-ID 哈希 | 结果 | 意图 | 工单状态 | 问题 |", "|---|---|---|---|---|",
    ]
    for case in run.get("cases") or []:
        lines.append(f"| `{case.get('message_id_sha256', '')[:16]}` | `{case.get('status')}` | `{case.get('actual_intent') or '-'}` | `{case.get('actual_status') or '-'}` | {', '.join(case.get('issues') or []) or '-'} |")
    lines.extend(["", "### 本轮问题与下一轮计划", ""])
    issues = sorted({issue for case in run.get("cases") or [] for issue in case.get("issues") or []})
    lines.extend([f"- [ ] {issue}" for issue in issues] or ["- 无新增问题。"])
    with ARCHIVE_DOC.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def run_suite(path: Path, confirm_suite: str) -> dict[str, Any]:
    validation = validate_manifest(path, require_approval=True)
    manifest = read_json(path)
    suite_id = str(manifest["suite_id"])
    if confirm_suite != suite_id:
        raise GoldCliError("SUITE_CONFIRMATION_MISMATCH")
    health = doctor(live=True)
    if health["status"] != "passed":
        raise GoldCliError("DOCTOR_BLOCKED", details=health)
    run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    run_root = suite_root(suite_id) / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    os.environ.setdefault("INTEGRATION_ADMIN_USERNAME", settings.DEFAULT_ADMIN_USERNAME)
    os.environ.setdefault("INTEGRATION_ADMIN_PASSWORD", settings.DEFAULT_ADMIN_PASSWORD)
    result: dict[str, Any] = {"schema_version": 1, "suite_id": suite_id, "run_id": run_id, "started_at": now_iso(), "status": "running", "cases": [], "actual_outbound_count": 0, "cleanup_status": "pending", "validation": validation}
    client = Client()
    initial: dict[str, Any] | None = None
    all_message_ids = [str(item["message_id"]) for item in manifest["messages"]]
    try:
        login(client)
        initial = current_config(client)
        patch_config(client, auto_send_enabled=False, auto_followup_enabled=False, rma_auto_send_enabled=False)
        for index, item in enumerate(manifest["messages"]):
            message_id = str(item["message_id"])
            case_root = run_root / f"case-{index + 1:03d}"
            case_root.mkdir(parents=True)
            case: dict[str, Any] = {"message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(), "status": "running", "issues": []}
            supplement_email_id: int | None = None
            result["cases"].append(case)
            mini = _mini_manifest(case_root, manifest, item)
            try:
                raw_uid, raw, _ = _fetch_raw_by_message_id(host=settings.IMAP_HOST, port=settings.IMAP_PORT, user=settings.IMAP_USER, password=settings.IMAP_PASSWORD, folder=settings.IMAP_FOLDER, message_id=message_id)
                del raw_uid
                if hashlib.sha256(raw).hexdigest() != item["raw_sha256"]:
                    raise GoldCliError("SOURCE_EML_HASH_CHANGED")
                asyncio.run(_reset([message_id], suite_id=suite_id, run_id=f"{run_id}-pre-{index + 1}", apply=True))
                _relay_reset()
                _relay_control("normal", item["gold"].get("fixed_rma_no"))
                temporary_state = case_root / "temporary-master-state.json"
                asyncio.run(apply_temporary_master_data(read_json(mini), temporary_state))
                baseline_uid = _rmatest2_max_uid()
                mode = item["gold"]["send_mode"]
                patch_config(client, auto_send_enabled=mode in {"auto_rma", "followup_then_rma"}, auto_followup_enabled=mode in {"auto_followup", "followup_then_rma"}, rma_auto_send_enabled=mode in {"auto_rma", "followup_then_rma"})
                email_id, fetch_result = _fetch_system_message(client, message_id)
                if email_id:
                    initial_status = "auto_replied" if mode == "followup_then_rma" else str(item["gold"].get("expected_final_status") or "")
                    value = _wait_for_case(client, email_id, initial_status, 1 if mode == "followup_then_rma" else int(item["gold"].get("expected_outbound_count") or 0))
                else:
                    precheck = fetch_result.get("precheck") or {}
                    rule = precheck.get("rule_analysis") or {}
                    value = {"email_detail": {"email": {"intent_type": fetch_result.get("intent_type") or rule.get("intent_type"), "intent_subtype": fetch_result.get("intent_subtype") or rule.get("intent_subtype"), "parse_status": fetch_result.get("fetch_status"), "terminal_reason_code": precheck.get("reason_code")}}, "ticket_detail": None}
                if mode == "followup_then_rma":
                    original_email_detail = value.get("email_detail")
                    first = _rmatest2_new_messages(baseline_uid)
                    matching_first = [row for row in first if row.get("in_reply_to") == message_id or message_id in row.get("references", "")]
                    if len(matching_first) != 1:
                        raise GoldCliError("FOLLOWUP_REPLY_NOT_UNIQUE", details={"match_count": len(matching_first)})
                    supplement_id = _send_supplement(message_id, matching_first[0], item["gold"]["supplement"])
                    supplement_email_id, _ = _fetch_system_message(client, supplement_id)
                    if not supplement_email_id:
                        raise GoldCliError("SUPPLEMENT_NOT_ARCHIVED")
                    recovered = _wait_for_case(client, supplement_email_id, str(item["gold"]["expected_final_status"]), int(item["gold"]["expected_outbound_count"]))
                    value = {"email_detail": original_email_detail, "ticket_detail": recovered.get("ticket_detail")}
                outbound = _rmatest2_new_messages(baseline_uid)
                graph_email_ids = [
                    value
                    for value in (email_id, supplement_email_id)
                    if value is not None
                ]
                graph_evidence = [
                    asyncio.run(_graph_execution_evidence(int(graph_email_id)))
                    for graph_email_id in dict.fromkeys(graph_email_ids)
                ] or [{"found": False, "interrupts": []}]
                case["graph_executions"] = graph_evidence
                case["issues"] = sorted(
                    set([
                        *_assert_case(item, value, outbound),
                        *[
                            issue
                            for evidence in graph_evidence
                            for issue in _assert_graph_execution_evidence(evidence)
                        ],
                    ])
                )
                email = (value.get("email_detail") or {}).get("email") or {}
                ticket = (value.get("ticket_detail") or {}).get("ticket") or {}
                case.update({"actual_intent": email.get("intent_type"), "actual_status": ticket.get("current_status_code"), "outbound_count": len(outbound), "status": "passed" if not case["issues"] else "failed"})
                result["actual_outbound_count"] += len(outbound)
            except Exception as exc:
                case["status"] = "error"
                case["issues"] = sorted(set([*case.get("issues", []), getattr(exc, "code", type(exc).__name__)]))
            finally:
                patch_config(client, auto_send_enabled=False, auto_followup_enabled=False, rma_auto_send_enabled=False)
                try:
                    asyncio.run(
                        cleanup_temporary_master_data(
                            mini,
                            state_path=case_root / "temporary-master-state.json",
                            skip_manifest_validation=True,
                        )
                    )
                except Exception as exc:
                    case["issues"] = sorted(set([*case.get("issues", []), f"TEMP_CLEANUP:{type(exc).__name__}"]))
                    case["status"] = "error"
                try:
                    asyncio.run(_reset([message_id], suite_id=suite_id, run_id=f"{run_id}-post-{index + 1}", apply=True))
                except Exception as exc:
                    case["issues"] = sorted(set([*case.get("issues", []), f"DB_CLEANUP:{getattr(exc, 'code', type(exc).__name__)}"]))
                    case["status"] = "error"
        if result["actual_outbound_count"] > int(manifest["max_actual_sends"]):
            raise GoldCliError("ACTUAL_SEND_HARD_LIMIT_EXCEEDED")
        result["status"] = "passed" if all(row["status"] == "passed" for row in result["cases"]) else "failed"
        result["previous_run_comparison"] = _compare_with_previous(run_root, result)
    except Exception as exc:
        result["status"] = "error"
        result["fatal_error"] = getattr(exc, "code", type(exc).__name__)
    finally:
        if initial is not None:
            try:
                patch_config(client, auto_send_enabled=bool(initial.get("auto_send_enabled")), auto_followup_enabled=bool(initial.get("auto_followup_enabled")), rma_auto_send_enabled=bool(initial.get("rma_auto_send_enabled")))
            except Exception as exc:
                result["runtime_restore_error"] = type(exc).__name__
                result["status"] = "error"
        try:
            final_cleanup = asyncio.run(_reset(all_message_ids, suite_id=suite_id, run_id=f"{run_id}-final", apply=True))
            _relay_reset()
            result["cleanup_status"] = "passed" if final_cleanup.get("verification", {}).get("verified") else "failed"
        except Exception as exc:
            result["cleanup_status"] = "failed"
            result["cleanup_error"] = getattr(exc, "code", type(exc).__name__)
            result["status"] = "error"
        result["finished_at"] = now_iso()
        write_json(run_root / "result.json", result)
        _append_archive(result)
    return result


def report(path: Path) -> dict[str, Any]:
    manifest = read_json(path)
    runs = sorted((suite_root(str(manifest["suite_id"])) / "runs").glob("*/result.json"))
    if not runs:
        raise GoldCliError("NO_RUN_RESULTS")
    latest = read_json(runs[-1])
    return {"status": "available", "archive_document": str(ARCHIVE_DOC), "latest_result": str(runs[-1]), "summary": {"run_status": latest.get("status"), "case_count": len(latest.get("cases") or []), "actual_outbound_count": latest.get("actual_outbound_count"), "cleanup_status": latest.get("cleanup_status")}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repeatable rmatest1/rmatest2 gold mail regression CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser("doctor", help="Check gates without sending mail")
    doctor_parser.add_argument("--live", action="store_true", help="Perform IMAP/SMTP login and NOOP checks; never sends mail")
    inv = sub.add_parser("inventory", help="Read exact rmatest1 originals with BODY.PEEK and create manifest")
    inv.add_argument("--suite-id", required=True)
    inv.add_argument("--message-id", action="append", required=True)
    val = sub.add_parser("validate", help="Validate manifest and optional approval hash")
    val.add_argument("--manifest", type=Path, required=True)
    val.add_argument("--require-approval", action="store_true")
    approve = sub.add_parser("approve", help="Approve the exact manifest SHA-256")
    approve.add_argument("--manifest", type=Path, required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--i-understand-real-mail", action="store_true")
    run = sub.add_parser("run", help="Execute, assert, archive and cleanup all cases")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--confirm-suite", required=True)
    clean = sub.add_parser("cleanup", help="Preview cleanup; --apply requires the preview plan hash")
    clean.add_argument("--manifest", type=Path, required=True)
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--plan-hash")
    rep = sub.add_parser("report", help="Return the latest archived run summary")
    rep.add_argument("--manifest", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "doctor":
            result = doctor(live=args.live)
        elif args.command == "inventory":
            result = inventory(args.suite_id, args.message_id)
        elif args.command == "validate":
            result = validate_manifest(args.manifest, require_approval=args.require_approval)
        elif args.command == "approve":
            result = approve_manifest(args.manifest, args.approved_by, args.i_understand_real_mail)
        elif args.command == "run":
            result = run_suite(args.manifest, args.confirm_suite)
        elif args.command == "cleanup":
            if args.apply and not args.plan_hash:
                raise GoldCliError("PLAN_HASH_REQUIRED_FOR_APPLY")
            result = cleanup(args.manifest, apply=args.apply, expected_hash=args.plan_hash)
        elif args.command == "report":
            result = report(args.manifest)
        else:
            raise GoldCliError("COMMAND_UNSUPPORTED")
        json_out({"ok": True, "command": args.command, "data": result})
        if isinstance(result, dict) and result.get("status") in {"blocked", "failed", "error"}:
            raise SystemExit(2)
    except (GoldCliError, GoldReplayError) as exc:
        json_out({"ok": False, "command": getattr(args, "command", None), "error": {"code": exc.code, "details": exc.details}})
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        json_out({"ok": False, "command": getattr(args, "command", None), "error": {"code": "INTERRUPTED"}})
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
