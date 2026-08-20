from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import imaplib
import json
import msvcrt
import os
import re
import smtplib
import ssl
import sys
import time
from contextlib import contextmanager
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
from app.core.email_classification import EmailIntent, INTENT_LEVEL
from app.core.database import AsyncSessionLocal, engine
from app.models import BoardCard, CustomerServicePolicy, SnAsset, User
from app.services.gold_replay import (
    GoldReplayError,
    apply_gold_test_reset,
    assert_gold_replay_environment,
    plan_gold_test_reset,
    verify_gold_test_reset,
)
from app.services.mail_safety import (
    TEST_MAIL_RECIPIENT,
    TEST_MAIL_SENDER,
    test_mail_configuration_reasons,
    test_only_subject,
)
from tools.run_new_repair_mail_e2e import Client, current_config, find_email, login, patch_config, wait_for_job
from tools.run_rmatest_batch_e2e import apply_temporary_master_data, cleanup_temporary_master_data


SCHEMA_VERSION = 3
MESSAGE_ID_PATTERN = re.compile(r"^<[^<>\s]+>$")
RMA_PATTERN = re.compile(r"^\d{10}$")
INTENTS = {str(intent) for intent in EmailIntent}
EXPECTED_LEVEL_BY_INTENT = {
    str(intent): str(INTENT_LEVEL[intent]) for intent in EmailIntent
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


@contextmanager
def _exclusive_suite_run(suite_id: str, run_id: str):
    """Prevent concurrent real-mail runners from mutating global switches."""
    lock_path = suite_root(suite_id) / ".real-mail-run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    locked = False
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError as exc:
            raise GoldCliError("REAL_MAIL_RUN_ALREADY_ACTIVE") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(
            json.dumps(
                {
                    "suite_id": suite_id,
                    "run_id": run_id,
                    "pid": os.getpid(),
                    "started_at": now_iso(),
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )
        stream.flush()
        yield
    finally:
        if locked:
            try:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        stream.close()


def _set_and_verify_config(
    client: Client,
    *,
    auto_send_enabled: bool,
    auto_followup_enabled: bool,
    rma_auto_send_enabled: bool,
) -> dict[str, Any]:
    expected = {
        "auto_send_enabled": auto_send_enabled,
        "auto_followup_enabled": auto_followup_enabled,
        "rma_auto_send_enabled": rma_auto_send_enabled,
    }
    patch_config(client, **expected)
    actual = current_config(client)
    drift = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) is not value
    }
    if drift:
        raise GoldCliError("RUNTIME_SEND_SWITCH_DRIFT", details={"drift": drift})
    return actual


def _assert_config_matches(client: Client, expected: dict[str, bool]) -> None:
    actual = current_config(client)
    drift = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) is not value
    }
    if drift:
        raise GoldCliError("RUNTIME_SEND_SWITCH_DRIFT", details={"drift": drift})


def _safe_exception_code(exc: Exception) -> str:
    explicit = getattr(exc, "code", None)
    if explicit:
        return str(explicit)
    message = str(exc).strip()
    if message and re.fullmatch(r"[A-Z0-9_]+(?::.*)?", message):
        return message.split(":", 1)[0]
    return type(exc).__name__


def json_out(payload: Any) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
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


def _classification_source_sha256(root: Path | None = None) -> str:
    """Hash code and route configuration that determine classification results."""
    source_root = root or (BACKEND_ROOT / "app")
    digest = hashlib.sha256()
    files = list(sorted(
        path
        for path in source_root.rglob("*.py")
        if "__pycache__" not in path.parts
    ))
    route_file = source_root.parent / "config" / "llm_routes.yaml"
    if route_file.is_file():
        files.append(route_file)
    for source_path in files:
        relative = (
            source_path.relative_to(source_root).as_posix()
            if source_path.is_relative_to(source_root)
            else f"config/{source_path.name}"
        ).encode("utf-8")
        payload = source_path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def run_database_async(awaitable: Any) -> Any:
    """Run one DB coroutine without leaking pooled asyncmy connections across loops.

    This CLI is synchronous and intentionally uses short event loops.  asyncmy
    connections are loop-bound on Windows, so the global SQLAlchemy pool must
    be disposed in the same loop before the next synchronous phase starts.
    """

    async def isolated() -> Any:
        try:
            return await awaitable
        finally:
            await engine.dispose()

    return asyncio.run(isolated())


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


def _safe_imap_logout(client: imaplib.IMAP4) -> None:
    """A server-side close after a successful read must not erase its result."""
    try:
        client.logout()
    except (imaplib.IMAP4.abort, OSError):
        pass


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
            "expected_final_fields": {},
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
        "max_system_outbound_sends": 0,
        "max_supplement_sends": 0,
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
        if intent not in INTENTS:
            errors.append(f"{prefix}.expected_intent_INVALID")
        if gold.get("expected_subtype") is not None:
            errors.append(f"{prefix}.expected_subtype_MUST_BE_NULL")
        if not isinstance(gold.get("create_ticket"), bool):
            errors.append(f"{prefix}.create_ticket_REQUIRED")
        if gold.get("send_mode") not in SEND_MODES:
            errors.append(f"{prefix}.send_mode_INVALID")
        expected_level = EXPECTED_LEVEL_BY_INTENT.get(str(intent))
        if gold.get("send_mode") != "none" and expected_level != "auto_repair":
            errors.append(f"{prefix}.SEND_MODE_REQUIRES_FIRST_INTENT")
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
        for key, expected_type in (
            ("expected_fields", dict),
            ("expected_final_fields", dict),
            ("expected_items", list),
            ("missing_fields", list),
        ):
            if not isinstance(gold.get(key), expected_type):
                errors.append(f"{prefix}.{key}_INVALID")
        expected_material_codes = {
            str(row.get("material_code") or "").strip()
            for row in gold.get("expected_items") or []
            if str(row.get("material_code") or "").strip()
        }
        for board_index, board in enumerate(
            gold.get("temporary_board_cards") or []
        ):
            material_code = str(board.get("material_code") or "").strip()
            if material_code not in expected_material_codes:
                errors.append(
                    f"{prefix}.temporary_board_cards[{board_index}].material_code_NOT_IN_EXPECTED_ITEMS"
                )
        if gold.get("create_ticket") and not gold.get("expected_final_status"):
            errors.append(f"{prefix}.expected_final_status_REQUIRED")
    supplement_sends = sum(
        1
        for item in messages
        if (item.get("gold") or {}).get("send_mode") == "followup_then_rma"
    )
    total_smtp_sends = planned_sends + supplement_sends
    if manifest.get("max_system_outbound_sends") != planned_sends:
        errors.append("MAX_SYSTEM_OUTBOUND_SENDS_MUST_EQUAL_PLANNED_SENDS")
    if manifest.get("max_supplement_sends") != supplement_sends:
        errors.append("MAX_SUPPLEMENT_SENDS_MUST_EQUAL_PLANNED_SUPPLEMENTS")
    if manifest.get("max_actual_sends") != total_smtp_sends:
        errors.append("MAX_ACTUAL_SENDS_MUST_EQUAL_ALL_PLANNED_SMTP_SENDS")
    approval_path = path.parent / "approval.json"
    approved = False
    if approval_path.exists():
        approval = read_json(approval_path)
        approved = approval.get("manifest_sha256") == file_sha256(path) and bool(approval.get("approved_by"))
    if require_approval and not approved:
        errors.append("UNCHANGED_MANIFEST_APPROVAL_REQUIRED")
    result = {
        "valid": not errors,
        "errors": errors,
        "message_count": len(messages),
        "planned_system_outbound_sends": planned_sends,
        "planned_supplement_sends": supplement_sends,
        "planned_total_smtp_sends": total_smtp_sends,
        "approved": approved,
    }
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


def _required_egress_destinations(manifest: dict[str, Any]) -> list[str]:
    destinations = ["project_oss", "deepseek_api"]
    if any(item.get("attachments") for item in manifest.get("messages") or []):
        destinations.append("qwen_api")
    return destinations


def authorize_sensitive_egress(
    path: Path, *, approved_by: str, acknowledge: bool
) -> dict[str, Any]:
    validate_manifest(path)
    if not acknowledge:
        raise GoldCliError("SENSITIVE_EGRESS_ACKNOWLEDGEMENT_REQUIRED")
    manifest = read_json(path)
    approval = {
        "schema_version": 1,
        "suite_id": manifest["suite_id"],
        "manifest_sha256": file_sha256(path),
        "destinations": _required_egress_destinations(manifest),
        "approved_by": approved_by,
        "approved_at": now_iso(),
    }
    target = suite_root(str(manifest["suite_id"])) / "sensitive-egress-approval.json"
    write_json(target, approval)
    return {
        "status": "approved",
        "approval": str(target),
        "manifest_sha256": approval["manifest_sha256"],
        "destinations": approval["destinations"],
    }


def _require_sensitive_egress_approval(
    path: Path, manifest: dict[str, Any]
) -> dict[str, Any]:
    target = suite_root(str(manifest["suite_id"])) / "sensitive-egress-approval.json"
    if not target.exists():
        raise GoldCliError("SENSITIVE_EGRESS_APPROVAL_REQUIRED")
    approval = read_json(target)
    if approval.get("manifest_sha256") != file_sha256(path):
        raise GoldCliError("SENSITIVE_EGRESS_APPROVAL_MANIFEST_CHANGED")
    if (
        approval.get("destinations") != _required_egress_destinations(manifest)
        or not approval.get("approved_by")
    ):
        raise GoldCliError("SENSITIVE_EGRESS_DESTINATIONS_NOT_APPROVED")
    return approval


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
    db = run_database_async(_database_doctor())
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
        result = await apply_gold_test_reset(
            session, message_ids=message_ids, expected_plan_hash=plan["plan_hash"],
            suite_id=suite_id, run_id=run_id, user_id=int(user_id),
            reason=f"Approved gold regression replay cleanup for {suite_id}/{run_id}",
        )
        result["verification"] = await verify_gold_test_reset(session, message_ids=message_ids)
        return result


def cleanup(path: Path, *, apply: bool, expected_hash: str | None) -> dict[str, Any]:
    manifest = read_json(path)
    message_ids = [str(item["message_id"]) for item in manifest.get("messages") or []]
    root = suite_root(str(manifest["suite_id"]))
    temporary_candidates = sorted(
        [
            *root.glob("runs/*/case-*/case-manifest.json"),
            *root.glob("classification-master-data/case-*/case-manifest.json"),
        ]
    )
    result = run_database_async(
        _reset(
            message_ids,
            suite_id=str(manifest["suite_id"]),
            run_id=f"cleanup-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            apply=False,
        )
    )
    temporary_plan = run_database_async(
        _plan_temporary_master_cleanup(
            temporary_candidates,
            expected_batch_id=str(manifest["suite_id"]),
        )
    )
    combined_plan = {
        "mail_plan_hash": result["plan_hash"],
        "temporary_master_plan": temporary_plan,
    }
    combined_plan_hash = hashlib.sha256(
        json.dumps(
            combined_plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    combined_blockers = sorted(
        set([*(result.get("blockers") or []), *(temporary_plan.get("blockers") or [])])
    )
    if apply:
        if expected_hash != combined_plan_hash:
            raise GoldCliError(
                "CLEANUP_PLAN_HASH_MISMATCH",
                details={"actual_plan_hash": combined_plan_hash},
            )
        if combined_blockers:
            raise GoldCliError("CLEANUP_BLOCKED", details={"blockers": combined_blockers})
        result = run_database_async(
            _reset(
                message_ids,
                suite_id=str(manifest["suite_id"]),
                run_id=f"cleanup-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                apply=True,
                expected_hash=result["plan_hash"],
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
                    "result": run_database_async(
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
        "cleanup_plan_hash": combined_plan_hash,
        "cleanup_blockers": combined_blockers,
        "temporary_master_plan": temporary_plan,
        "temporary_master_state_files": len(temporary_candidates),
        "temporary_master_cleanup": temporary_results,
        "relay_reset": bool(apply),
    }


async def _plan_temporary_master_cleanup(
    candidates: list[Path], *, expected_batch_id: str
) -> dict[str, Any]:
    delete_ids: dict[str, set[int]] = {
        "sn_assets": set(),
        "board_cards": set(),
        "customer_policies": set(),
    }
    restore_ids: dict[str, set[int]] = {
        "sn_assets": set(),
        "board_cards": set(),
    }
    state_hashes: list[str] = []
    blockers: list[str] = []
    async with AsyncSessionLocal() as session:
        for candidate in candidates:
            state_path = candidate.parent / "temporary-master-state.json"
            if not state_path.exists():
                continue
            state_hashes.append(file_sha256(state_path))
            state = read_json(state_path)
            if str(state.get("batch_id") or "") != expected_batch_id:
                blockers.append("TEMPORARY_MASTER_BATCH_ID_MISMATCH")
                continue
            created = state.get("temporary_master_data") or {}
            for name, model, state_key in (
                ("sn_assets", SnAsset, "sn_asset_ids"),
                ("board_cards", BoardCard, "board_card_ids"),
                ("customer_policies", CustomerServicePolicy, "customer_policy_ids"),
            ):
                ids = sorted({int(value) for value in created.get(state_key) or []})
                if not ids:
                    continue
                rows = list(
                    (
                        await session.execute(select(model).where(model.id.in_(ids)))
                    ).scalars()
                )
                delete_ids[name].update(
                    int(row.id)
                    for row in rows
                    if row.source_file_name == expected_batch_id
                )
            for name, model, state_key in (
                ("sn_assets", SnAsset, "overridden_sn_assets"),
                ("board_cards", BoardCard, "overridden_board_cards"),
            ):
                snapshots = list(created.get(state_key) or [])
                ids = sorted(
                    {int(row.get("id") or 0) for row in snapshots if row.get("id")}
                )
                if not ids:
                    continue
                rows = list(
                    (
                        await session.execute(select(model).where(model.id.in_(ids)))
                    ).scalars()
                )
                restore_ids[name].update(
                    int(row.id)
                    for row in rows
                    if row.source_file_name == expected_batch_id
                )
    return {
        "state_file_count": len(state_hashes),
        "state_file_hashes": sorted(state_hashes),
        "delete_ids": {name: sorted(values) for name, values in delete_ids.items()},
        "restore_ids": {name: sorted(values) for name, values in restore_ids.items()},
        "blockers": sorted(set(blockers)),
    }


def _fetch_system_message(
    client: Client,
    message_id: str,
    *,
    availability_timeout_seconds: int = 90,
    expected_switches: dict[str, bool] | None = None,
) -> tuple[int | None, dict[str, Any]]:
    deadline = time.monotonic() + availability_timeout_seconds
    last_match_count = 0
    while True:
        if expected_switches is not None:
            _assert_config_matches(client, expected_switches)
        response = client.data("POST", "/api/v1/emails/fetch/jobs", params={"folder_name": "INBOX", "limit": 1, "unseen_only": "false", "message_id": message_id, "auto_parse": "true"})
        job_payload = response.get("job") if isinstance(response, dict) else None
        if not isinstance(job_payload, dict) or not job_payload.get("id") or response.get("reused"):
            raise GoldCliError("IMAP_FETCH_JOB_NOT_CREATED")
        job = wait_for_job(client, int(job_payload["id"]))
        fetched = (job.get("result_json") or {}).get("fetched") or []
        match = [row for row in fetched if row.get("message_id") == message_id]
        last_match_count = len(match)
        if len(match) == 1:
            email_id = match[0].get("email_id")
            if not email_id:
                found = find_email(client, message_id)
                email_id = found.get("id") if found else None
            return int(email_id) if email_id else None, match[0]
        if len(match) > 1 or time.monotonic() >= deadline:
            raise GoldCliError(
                "IMAP_EXACT_FETCH_RESULT_INVALID",
                details={"match_count": last_match_count},
            )
        # SMTP acceptance can precede IMAP visibility on the test provider.
        time.sleep(2)


def _resume_pending_reply_after_enabling(
    client: Client,
    *,
    email_id: int,
    ticket_detail: dict[str, Any],
) -> None:
    """Re-enter template creation so a zero-send baseline draft can auto-send.

    The classification gate intentionally creates drafts with sending disabled.
    Replaying the same Message-ID later restores that draft from the baseline;
    calling the idempotent draft endpoint activates it under the now-enabled
    runtime switches without creating a second reply.
    """
    ticket = ticket_detail.get("ticket") or {}
    if ticket.get("current_status_code") != "need_customer_info":
        return
    pending = [
        row
        for row in ticket_detail.get("reply_records") or []
        if row.get("reply_type") in {"missing_fields", "followup"}
        and row.get("send_status") == "pending_review"
    ]
    if len(pending) != 1:
        return
    client.data(
        "POST",
        f"/api/v1/replies/{int(ticket['id'])}/draft",
        body={
            "reply_type": pending[0]["reply_type"],
            "related_email_id": email_id,
            "language": ticket.get("language_code") or "zh-CN",
            "missing_fields": ticket.get("missing_fields") or {},
        },
    )


def _wait_for_parse_terminal(
    client: Client,
    email_id: int,
    *,
    timeout_seconds: int = 300,
    expected_switches: dict[str, bool] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if expected_switches is not None:
            _assert_config_matches(client, expected_switches)
        email_detail = client.data("GET", f"/api/v1/emails/{email_id}")
        last = email_detail
        email = email_detail.get("email") or {}
        parse_results = email_detail.get("parse_results") or []
        ticket_ids = [row.get("ticket_id") for row in parse_results if row.get("ticket_id")]
        if ticket_ids:
            return (
                email_detail,
                client.data("GET", f"/api/v1/tickets/{int(ticket_ids[0])}"),
            )
        parse_status = str(email.get("parse_status") or "")
        if parse_status in TERMINAL_PARSE_STATUSES:
            return email_detail, None
        time.sleep(2)
    raise GoldCliError(
        "CLASSIFICATION_PARSE_TIMEOUT",
        details={
            "email_id": email_id,
            "parse_status": (last.get("email") or {}).get("parse_status"),
        },
    )


def _wait_for_gold_jobs_idle(
    message_id: str, *, timeout_seconds: int = 300
) -> dict[str, Any]:
    """Keep temporary master data alive through every downstream local job."""
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        async def load_plan() -> dict[str, Any]:
            async with AsyncSessionLocal() as session:
                return await plan_gold_test_reset(session, message_ids=[message_id])

        last = run_database_async(load_plan())
        if "GOLD_REPLAY_ACTIVE_JOBS" not in (last.get("blockers") or []):
            return last
        time.sleep(2)
    raise GoldCliError(
        "CLASSIFICATION_DOWNSTREAM_JOBS_TIMEOUT",
        details={"active_job_ids": last.get("active_job_ids") or []},
    )


def classify_suite(path: Path, confirm_suite: str) -> dict[str, Any]:
    """Run every frozen source through the real parser with all sending off."""
    manifest = read_json(path)
    suite_id = str(manifest.get("suite_id") or "")
    if confirm_suite != suite_id:
        raise GoldCliError("SUITE_CONFIRMATION_MISMATCH")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise GoldCliError("MANIFEST_SCHEMA_INVALID")
    egress_approval = _require_sensitive_egress_approval(path, manifest)
    os.environ.setdefault("INTEGRATION_ADMIN_USERNAME", settings.DEFAULT_ADMIN_USERNAME)
    os.environ.setdefault("INTEGRATION_ADMIN_PASSWORD", settings.DEFAULT_ADMIN_PASSWORD)
    client = Client()
    login(client)
    initial = current_config(client)
    result: dict[str, Any] = {
        "status": "running",
        "suite_id": suite_id,
        "started_at": now_iso(),
        "messages_sent": 0,
        "sensitive_egress_approval": {
            "manifest_sha256": egress_approval["manifest_sha256"],
            "destinations": egress_approval["destinations"],
            "approved_by": egress_approval["approved_by"],
        },
        "cases": [],
    }
    evidence = suite_root(suite_id) / "classification-baseline.json"
    try:
        patch_config(
            client,
            auto_send_enabled=False,
            auto_followup_enabled=False,
            rma_auto_send_enabled=False,
        )
        for index, item in enumerate(manifest.get("messages") or []):
            message_id = str(item["message_id"])
            case_root = suite_root(suite_id) / "classification-master-data" / f"case-{index + 1:03d}"
            case_root.mkdir(parents=True, exist_ok=True)
            mini = _mini_manifest(case_root, manifest, item)
            temporary_state = case_root / "temporary-master-state.json"
            raw_uid, raw, _ = _fetch_raw_by_message_id(
                host=settings.IMAP_HOST,
                port=settings.IMAP_PORT,
                user=settings.IMAP_USER,
                password=settings.IMAP_PASSWORD,
                folder=settings.IMAP_FOLDER,
                message_id=message_id,
            )
            del raw_uid
            if hashlib.sha256(raw).hexdigest() != item.get("raw_sha256"):
                raise GoldCliError("SOURCE_EML_HASH_CHANGED")
            run_database_async(
                _reset(
                    [message_id],
                    suite_id=suite_id,
                    run_id=f"classification-pre-{index + 1}",
                    apply=True,
                )
            )
            try:
                run_database_async(
                    apply_temporary_master_data(
                        read_json(mini),
                        temporary_state,
                        allow_gold_e2e_snapshot_override=True,
                    )
                )
                email_id, fetch_result = _fetch_system_message(client, message_id)
                if email_id is None:
                    result["cases"].append(
                        {
                            "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
                            "email_id": None,
                            "intent_type": fetch_result.get("intent_type"),
                            "handling_level": fetch_result.get("handling_level"),
                            "parse_status": fetch_result.get("fetch_status"),
                            "ticket": None,
                            "items": [],
                        }
                    )
                    continue
                _wait_for_parse_terminal(client, email_id)
                _wait_for_gold_jobs_idle(message_id)
                email_detail, ticket_detail = _wait_for_parse_terminal(client, email_id)
                email = email_detail.get("email") or {}
                ticket = (ticket_detail or {}).get("ticket") or None
                result["cases"].append({
                    "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
                    "email_id": email_id,
                    "intent_type": email.get("intent_type"),
                    "handling_level": email.get("handling_level"),
                    "persistence_tier": email.get("persistence_tier"),
                    "classification_reason_code": email.get("classification_reason_code"),
                    "parse_status": email.get("parse_status"),
                    "ticket": (
                        {
                            "id": ticket.get("id"),
                            "status": ticket.get("current_status_code"),
                            "customer_code": ticket.get("customer_code"),
                            "customer_name": ticket.get("customer_name"),
                            "contact_person": ticket.get("contact_person"),
                            "contact_phone": ticket.get("contact_phone"),
                            "mailing_address": ticket.get("mailing_address"),
                            "problem_description": ticket.get("problem_description"),
                            "missing_fields": ticket.get("missing_fields"),
                            "language_code": ticket.get("language_code"),
                        }
                        if ticket
                        else None
                    ),
                    "items": [
                        {
                            "sn": row.get("sn"),
                            "material_code": row.get("material_code"),
                            "material_name": row.get("material_name"),
                            "failure_description": row.get("failure_description"),
                        }
                        for row in (ticket_detail or {}).get("items") or []
                    ],
                    "reply_records": [
                        {
                            "reply_type": row.get("reply_type"),
                            "send_status": row.get("send_status"),
                            "missing_fields": row.get("missing_fields"),
                            "template_id": row.get("template_id"),
                        }
                        for row in (ticket_detail or {}).get("reply_records") or []
                    ],
                })
            finally:
                try:
                    run_database_async(
                        cleanup_temporary_master_data(
                            mini,
                            state_path=temporary_state,
                            skip_manifest_validation=True,
                        )
                    )
                finally:
                    run_database_async(
                        _reset(
                            [message_id],
                            suite_id=suite_id,
                            run_id=f"classification-post-{index + 1}",
                            apply=True,
                        )
                    )
        classification_issues = _classification_issues(manifest, result)
        result["issues"] = classification_issues
        result["status"] = "passed" if not classification_issues else "failed"
        result["finished_at"] = now_iso()
        write_json(evidence, result)
        gate = {
            "schema_version": 1,
            "suite_id": suite_id,
            "manifest_sha256": file_sha256(path),
            "classification_source_sha256": _classification_source_sha256(),
            "classification_evidence_sha256": file_sha256(evidence),
            "status": result["status"],
            "checked_at": now_iso(),
        }
        write_json(suite_root(suite_id) / "classification-gate.json", gate)
        return {**result, "evidence": str(evidence)}
    finally:
        patch_config(
            client,
            auto_send_enabled=bool(initial.get("auto_send_enabled")),
            auto_followup_enabled=bool(initial.get("auto_followup_enabled")),
            rma_auto_send_enabled=bool(initial.get("rma_auto_send_enabled")),
        )


def _wait_for_case(
    client: Client,
    email_id: int,
    expected_status: str,
    expected_outbound: int,
    timeout_seconds: int = 300,
    *,
    approve_special_policy: bool = False,
    expected_switches: dict[str, bool] | None = None,
    max_sent_followups: int | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    policy_approval_requested = False
    approved_reply_ids: set[int] = set()
    while time.monotonic() < deadline:
        if expected_switches is not None:
            _assert_config_matches(client, expected_switches)
        email_detail = client.data("GET", f"/api/v1/emails/{email_id}")
        ticket_ids = [row.get("ticket_id") for row in email_detail.get("parse_results") or [] if row.get("ticket_id")]
        if ticket_ids:
            ticket_detail = client.data("GET", f"/api/v1/tickets/{int(ticket_ids[0])}")
            ticket = ticket_detail.get("ticket") or {}
            if approve_special_policy and not policy_approval_requested:
                special_tasks = [
                    row
                    for row in ticket_detail.get("manual_tasks") or []
                    if row.get("task_type") == "rma_special_policy_review"
                    and row.get("status") not in {"resolved", "closed", "cancelled"}
                ]
                if special_tasks:
                    client.data(
                        "POST",
                        f"/api/v1/tickets/{int(ticket['id'])}/rma/manual-policy-approve",
                        body={
                            "reason": "Gold regression: verified special price and currency against approved manifest.",
                            "confirm_policy_values": True,
                            "confirm_template_thread_and_archive": True,
                        },
                    )
                    policy_approval_requested = True
            if approve_special_policy and policy_approval_requested:
                for row in ticket_detail.get("reply_records") or []:
                    reply_id = int(row.get("id") or 0)
                    if (
                        reply_id
                        and reply_id not in approved_reply_ids
                        and row.get("reply_type") == "rma_authorization"
                        and row.get("send_status") == "pending_review"
                    ):
                        client.data(
                            "POST",
                            f"/api/v1/replies/{reply_id}/approve-send/jobs",
                        )
                        approved_reply_ids.add(reply_id)
            sent = [row for row in ticket_detail.get("reply_records") or [] if row.get("send_status") == "sent"]
            if max_sent_followups is not None:
                sent_followups = [
                    row
                    for row in sent
                    if row.get("reply_type") in {"missing_fields", "followup"}
                ]
                if len(sent_followups) > max_sent_followups:
                    raise GoldCliError(
                        "FOLLOWUP_NO_PROGRESS",
                        details={"sent_followup_count": len(sent_followups)},
                    )
            last = {"email_detail": email_detail, "ticket_detail": ticket_detail}
            if _status_satisfies_expected(
                ticket.get("current_status_code"), expected_status
            ) and len(sent) >= expected_outbound:
                return last
        else:
            last = {"email_detail": email_detail, "ticket_detail": None}
            email = email_detail.get("email") or {}
            if not expected_status and email.get("parse_status") in TERMINAL_PARSE_STATUSES:
                return last
        time.sleep(2)
    raise GoldCliError("CASE_TIMEOUT", details={"last": last})


def _status_satisfies_expected(actual: str | None, expected: str | None) -> bool:
    """Accept evidence-gated closure as the completed successor of rma_sent."""
    return actual == expected or (expected == "rma_sent" and actual == "closed")


def _mailbox_max_uid(
    *, host: str, port: int, user: str, password: str, folder: str, use_ssl: bool
) -> int:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _mailbox_max_uid_once(
                host=host,
                port=port,
                user=user,
                password=password,
                folder=folder,
                use_ssl=use_ssl,
            )
        except (imaplib.IMAP4.abort, OSError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _mailbox_max_uid_once(
    *, host: str, port: int, user: str, password: str, folder: str, use_ssl: bool
) -> int:
    client = _imap_connect(
        host=host, port=port, user=user, password=password, use_ssl=use_ssl
    )
    try:
        client.select(folder, readonly=True)
        status, data = client.uid("search", None, "ALL")
        uids = [int(value) for value in (data[0] or b"").split()] if status == "OK" else []
        return max(uids, default=0)
    finally:
        _safe_imap_logout(client)


def _mailbox_new_messages(
    after_uid: int,
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    folder: str,
    use_ssl: bool,
    evidence_dir: Path | None = None,
    evidence_thread_message_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _mailbox_new_messages_once(
                after_uid,
                host=host,
                port=port,
                user=user,
                password=password,
                folder=folder,
                use_ssl=use_ssl,
                evidence_dir=evidence_dir,
                evidence_thread_message_ids=evidence_thread_message_ids,
            )
        except (imaplib.IMAP4.abort, OSError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _mailbox_new_messages_once(
    after_uid: int,
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    folder: str,
    use_ssl: bool,
    evidence_dir: Path | None = None,
    evidence_thread_message_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    client = _imap_connect(
        host=host, port=port, user=user, password=password, use_ssl=use_ssl
    )
    try:
        client.select(folder, readonly=True)
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
            plain_part = msg.get_body(preferencelist=("plain",))
            html_part = msg.get_body(preferencelist=("html",))
            plain_body = str(plain_part.get_content() if plain_part is not None else "")
            html_body = str(html_part.get_content() if html_part is not None else "")
            attachments = []
            in_reply_to = str(msg.get("In-Reply-To") or "")
            references = str(msg.get("References") or "")
            save_evidence = evidence_dir is not None and any(
                message_id == in_reply_to or message_id in references
                for message_id in (evidence_thread_message_ids or set())
            )
            if save_evidence:
                evidence_dir.mkdir(parents=True, exist_ok=True)
                (evidence_dir / f"uid-{int(uid)}.eml").write_bytes(raw)
            evidence_parts = [
                part
                for part in msg.walk()
                if not part.is_multipart()
                and (
                    part.get_content_disposition() == "attachment"
                    or bool(part.get("Content-ID"))
                )
            ]
            for attachment_index, part in enumerate(evidence_parts, start=1):
                payload = part.get_payload(decode=True) or b""
                attachment = {
                    "filename": part.get_filename(),
                    "content_type": part.get_content_type(),
                    "content_id": str(part.get("Content-ID") or ""),
                    "content_disposition": str(part.get_content_disposition() or ""),
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                if save_evidence and part.get_content_type() == "application/pdf":
                    pdf_path = evidence_dir / f"uid-{int(uid)}-attachment-{attachment_index}.pdf"
                    pdf_path.write_bytes(payload)
                    attachment["evidence_path"] = str(pdf_path)
                attachments.append(attachment)
            result.append({"uid": int(uid), "message_id": str(msg.get("Message-ID") or ""), "subject": str(msg.get("Subject") or ""), "from": str(msg.get("From") or ""), "to": str(msg.get("To") or ""), "cc": str(msg.get("Cc") or ""), "date": str(msg.get("Date") or ""), "in_reply_to": str(msg.get("In-Reply-To") or ""), "references": str(msg.get("References") or ""), "attachments": attachments, "_plain_body": plain_body, "_html_body": html_body})
        return result
    finally:
        _safe_imap_logout(client)


def _rmatest2_max_uid() -> int:
    return _mailbox_max_uid(
        host=settings.E2E_RMATEST2_IMAP_HOST,
        port=settings.E2E_RMATEST2_IMAP_PORT,
        user=settings.E2E_RMATEST2_IMAP_USER,
        password=settings.E2E_RMATEST2_IMAP_PASSWORD,
        folder=settings.E2E_RMATEST2_IMAP_FOLDER,
        use_ssl=settings.E2E_RMATEST2_IMAP_USE_SSL,
    )


def _rmatest1_max_uid() -> int:
    return _mailbox_max_uid(
        host=settings.IMAP_HOST,
        port=settings.IMAP_PORT,
        user=settings.IMAP_USER,
        password=settings.IMAP_PASSWORD,
        folder=settings.IMAP_FOLDER,
        use_ssl=True,
    )


def _rmatest2_new_messages(
    after_uid: int,
    evidence_dir: Path | None = None,
    evidence_thread_message_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    return _mailbox_new_messages(
        after_uid,
        host=settings.E2E_RMATEST2_IMAP_HOST,
        port=settings.E2E_RMATEST2_IMAP_PORT,
        user=settings.E2E_RMATEST2_IMAP_USER,
        password=settings.E2E_RMATEST2_IMAP_PASSWORD,
        folder=settings.E2E_RMATEST2_IMAP_FOLDER,
        use_ssl=settings.E2E_RMATEST2_IMAP_USE_SSL,
        evidence_dir=evidence_dir,
        evidence_thread_message_ids=evidence_thread_message_ids,
    )


def _rmatest1_new_messages(
    after_uid: int,
    evidence_dir: Path | None = None,
    evidence_thread_message_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    return _mailbox_new_messages(
        after_uid,
        host=settings.IMAP_HOST,
        port=settings.IMAP_PORT,
        user=settings.IMAP_USER,
        password=settings.IMAP_PASSWORD,
        folder=settings.IMAP_FOLDER,
        use_ssl=True,
        evidence_dir=evidence_dir,
        evidence_thread_message_ids=evidence_thread_message_ids,
    )


def _wait_for_case_outbound(
    baseline_uid: int,
    *,
    original_message_id: str,
    expected_count: int,
    client: Client,
    expected_switches: dict[str, bool],
    timeout_seconds: int = 90,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    matching: list[dict[str, Any]] = []
    while True:
        _assert_config_matches(client, expected_switches)
        outbound = _rmatest2_new_messages(baseline_uid)
        matching = [
            row
            for row in outbound
            if row.get("in_reply_to") == original_message_id
            or original_message_id in str(row.get("references") or "")
        ]
        if len(matching) >= expected_count:
            return outbound
        if time.monotonic() >= deadline:
            return outbound
        time.sleep(2)


def _suite_mailbox_counts(
    *,
    rmatest2_baseline_uid: int,
    rmatest1_baseline_uid: int,
    original_message_ids: list[str],
    supplement_message_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return mailbox-authoritative sends for only this run's threads."""
    system_messages = [
        row
        for row in _rmatest2_new_messages(rmatest2_baseline_uid)
        if TEST_MAIL_SENDER.lower() in str(row.get("from") or "").lower()
        and TEST_MAIL_RECIPIENT.lower() in str(row.get("to") or "").lower()
        and any(
            message_id == row.get("in_reply_to")
            or message_id in str(row.get("references") or "")
            for message_id in original_message_ids
        )
    ]
    supplement_messages = [
        row
        for row in _rmatest1_new_messages(rmatest1_baseline_uid)
        if row.get("message_id") in supplement_message_ids
    ]
    return system_messages, supplement_messages


def _refresh_authoritative_send_counts(
    result: dict[str, Any],
    *,
    rmatest2_baseline_uid: int,
    rmatest1_baseline_uid: int,
    original_message_ids: list[str],
    supplement_message_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system_messages, supplement_messages = _suite_mailbox_counts(
        rmatest2_baseline_uid=rmatest2_baseline_uid,
        rmatest1_baseline_uid=rmatest1_baseline_uid,
        original_message_ids=original_message_ids,
        supplement_message_ids=supplement_message_ids,
    )
    result["actual_system_outbound_count"] = len(system_messages)
    result["actual_supplement_send_count"] = len(supplement_messages)
    result["actual_total_smtp_count"] = len(system_messages) + len(
        supplement_messages
    )
    return system_messages, supplement_messages


def _send_supplement(
    original_message_id: str,
    reply_message: dict[str, Any],
    supplement: dict[str, Any],
    *,
    sent_so_far: int,
    hard_limit: int,
) -> str:
    if sent_so_far >= hard_limit:
        raise GoldCliError("SUPPLEMENT_SEND_HARD_LIMIT_EXCEEDED")
    message_id = make_msgid(domain="accotest.com")
    msg = EmailMessage()
    msg["From"] = TEST_MAIL_RECIPIENT
    msg["To"] = TEST_MAIL_SENDER
    msg["Subject"] = test_only_subject(
        str(supplement.get("subject") or f"Re: {reply_message.get('subject') or ''}")
    )
    msg["Message-ID"] = message_id
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["In-Reply-To"] = str(reply_message.get("message_id") or original_message_id)
    msg["References"] = " ".join(dict.fromkeys([original_message_id, str(reply_message.get("message_id") or "")]))
    body_text = str(supplement.get("body_text") or "").strip()
    if not body_text:
        raise GoldCliError("SUPPLEMENT_BODY_REQUIRED")
    replied_plain = str(reply_message.get("_plain_body") or "").rstrip()
    if not replied_plain:
        raise GoldCliError("SUPPLEMENT_QUOTED_SOURCE_REQUIRED")
    quote_header = (
        f"On {reply_message.get('date') or ''}, "
        f"{reply_message.get('from') or TEST_MAIL_SENDER} wrote:"
    )
    quoted_plain = "\n".join(f"> {line}" if line else ">" for line in replied_plain.splitlines())
    msg.set_content(f"{body_text}\n\n{quote_header}\n{quoted_plain}")
    replied_html = str(reply_message.get("_html_body") or "").strip()
    new_html = html.escape(body_text).replace("\n", "<br>\n")
    quote_html = replied_html or html.escape(replied_plain).replace("\n", "<br>\n")
    msg.add_alternative(
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">'
        f"{new_html}<br><br>{html.escape(quote_header)}"
        f'<blockquote style="margin:8px 0 0 12px;border-left:2px solid #ccc;padding-left:10px">'
        f"{quote_html}</blockquote></div>",
        subtype="html",
    )
    recipients = [address.lower() for _, address in getaddresses([str(msg["To"])])]
    if (
        str(msg["From"]).lower() != TEST_MAIL_RECIPIENT
        or recipients != [TEST_MAIL_SENDER]
        or msg.get("Cc")
        or msg.get("Bcc")
        or not str(msg["Subject"]).upper().startswith("[TEST ONLY]")
    ):
        raise GoldCliError("SUPPLEMENT_ENVELOPE_GATE_FAILED")
    with smtplib.SMTP_SSL(settings.E2E_RMATEST2_SMTP_HOST, settings.E2E_RMATEST2_SMTP_PORT, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(settings.E2E_RMATEST2_SMTP_USER, settings.E2E_RMATEST2_SMTP_PASSWORD)
        smtp.send_message(msg, from_addr=TEST_MAIL_RECIPIENT, to_addrs=[TEST_MAIL_SENDER])
    return message_id


def _assert_case(item: dict[str, Any], value: dict[str, Any], outbound: list[dict[str, Any]]) -> list[str]:
    gold = item["gold"]
    issues: list[str] = []
    email = (value.get("email_detail") or {}).get("email") or {}
    classification = email or value.get("fetch_result") or {}
    ticket_detail = value.get("ticket_detail") or {}
    ticket = ticket_detail.get("ticket") or {}
    if classification.get("intent_type") != gold.get("expected_intent"):
        issues.append("INTENT_MISMATCH")
    expected_level = EXPECTED_LEVEL_BY_INTENT.get(str(gold.get("expected_intent")))
    if classification.get("handling_level") != expected_level:
        issues.append("HANDLING_LEVEL_MISMATCH")
    expected_tier = "business" if expected_level == "auto_repair" else "minimal"
    if expected_level == "lifecycle_only":
        if email:
            issues.append("THIRD_CREATED_EMAIL")
    elif email.get("persistence_tier") != expected_tier:
        issues.append("PERSISTENCE_TIER_MISMATCH")
    if bool(ticket) != bool(gold.get("create_ticket")):
        issues.append("TICKET_CREATION_MISMATCH")
    if ticket and not _status_satisfies_expected(
        ticket.get("current_status_code"), gold.get("expected_final_status")
    ):
        issues.append("FINAL_STATUS_MISMATCH")
    final_fields = gold.get("expected_final_fields") or gold.get("expected_fields") or {}
    for key, expected in final_fields.items():
        if ticket.get(key) != expected:
            issues.append(f"FIELD_MISMATCH:{key}")
    if ticket:
        actual_missing = sorted((ticket.get("missing_fields") or {}).keys())
        expected_final_missing = (
            []
            if gold.get("send_mode") == "followup_then_rma"
            else sorted(gold.get("missing_fields") or [])
        )
        if actual_missing != expected_final_missing:
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
        rma_replies = [row for row in sent_replies if row.get("reply_type") == "rma_authorization"]
        expected_rma_reply_version = str(gold.get("expected_rma_reply_version") or "")
        if expected_rma_reply_version and any(
            row.get("reply_template_version") != expected_rma_reply_version for row in rma_replies
        ):
            issues.append("RMA_REPLY_TEMPLATE_VERSION_MISMATCH")
        for row in rma_replies:
            snapshot = row.get("rma_pdf_data_snapshot") or {}
            snapshot_data = snapshot.get("data") or snapshot
            expected_contact = str(final_fields.get("contact_person") or "")
            expected_phone = str(final_fields.get("contact_phone") or "")
            if expected_contact and snapshot_data.get("mailing_contact_person") != expected_contact:
                issues.append("RMA_PDF_CONTACT_PERSON_MISMATCH")
            if expected_phone and snapshot_data.get("mailing_contact_phone") != expected_phone:
                issues.append("RMA_PDF_CONTACT_PHONE_MISMATCH")
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
        if not any(
            "accotest_logo" in str(attachment.get("content_id") or "")
            for attachment in row.get("attachments") or []
        ):
            issues.append("OUTBOUND_SIGNATURE_LOGO_MISSING")
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


def _classification_issues(
    manifest: dict[str, Any], result: dict[str, Any]
) -> list[dict[str, Any]]:
    """Compare the zero-send parse with the exact business gold before run."""
    actual_by_hash = {
        row.get("message_id_sha256"): row for row in result.get("cases") or []
    }
    issues: list[dict[str, Any]] = []
    for item in manifest.get("messages") or []:
        message_id = str(item["message_id"])
        message_hash = hashlib.sha256(message_id.encode()).hexdigest()
        actual = actual_by_hash.get(message_hash)
        gold = item["gold"]
        codes: list[str] = []
        if actual is None:
            codes.append("CLASSIFICATION_RESULT_MISSING")
        else:
            if actual.get("intent_type") != gold.get("expected_intent"):
                codes.append("INTENT_MISMATCH")
            expected_level = EXPECTED_LEVEL_BY_INTENT.get(str(gold.get("expected_intent")))
            if actual.get("handling_level") != expected_level:
                codes.append("HANDLING_LEVEL_MISMATCH")
            expected_tier = "business" if expected_level == "auto_repair" else "minimal"
            if expected_level == "lifecycle_only":
                if actual.get("email_id") is not None:
                    codes.append("THIRD_CREATED_EMAIL")
            elif actual.get("persistence_tier") != expected_tier:
                codes.append("PERSISTENCE_TIER_MISMATCH")
            ticket = actual.get("ticket") or None
            if bool(ticket) != bool(gold.get("create_ticket")):
                codes.append("TICKET_CREATION_MISMATCH")
            if ticket:
                expected_stage = (
                    "need_customer_info"
                    if gold.get("missing_fields")
                    else "ready_for_export"
                )
                if ticket.get("status") != expected_stage:
                    codes.append("CLASSIFICATION_STAGE_MISMATCH")
                missing = sorted((ticket.get("missing_fields") or {}).keys())
                if missing != sorted(gold.get("missing_fields") or []):
                    codes.append("MISSING_FIELDS_MISMATCH")
                for key, expected in (gold.get("expected_fields") or {}).items():
                    if ticket.get(key) != expected:
                        codes.append(f"FIELD_MISMATCH:{key}")
                expected_sns = sorted(
                    str(row.get("sn") or "").strip().upper()
                    for row in gold.get("expected_items") or []
                    if row.get("sn")
                )
                actual_sns = sorted(
                    str(row.get("sn") or "").strip().upper()
                    for row in actual.get("items") or []
                    if row.get("sn")
                )
                if actual_sns != expected_sns:
                    codes.append("SN_SET_MISMATCH")
                for expected_item in gold.get("expected_items") or []:
                    if not any(
                        all(row.get(key) == expected for key, expected in expected_item.items())
                        for row in actual.get("items") or []
                    ):
                        codes.append("ITEM_FIELD_MISMATCH")
                        break
        if codes:
            issues.append(
                {
                    "message_id_sha256": message_hash,
                    "codes": sorted(set(codes)),
                }
            )
    return issues


def _require_classification_gate(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    gate_path = suite_root(str(manifest["suite_id"])) / "classification-gate.json"
    if not gate_path.exists():
        raise GoldCliError("PASSED_CLASSIFICATION_GATE_REQUIRED")
    gate = read_json(gate_path)
    if gate.get("manifest_sha256") != file_sha256(path):
        raise GoldCliError("CLASSIFICATION_GATE_MANIFEST_CHANGED")
    if gate.get("status") != "passed":
        raise GoldCliError("CLASSIFICATION_GATE_NOT_PASSED")
    if gate.get("classification_source_sha256") != _classification_source_sha256():
        raise GoldCliError("CLASSIFICATION_SOURCE_CHANGED")
    evidence = suite_root(str(manifest["suite_id"])) / "classification-baseline.json"
    if not evidence.exists() or gate.get("classification_evidence_sha256") != file_sha256(evidence):
        raise GoldCliError("CLASSIFICATION_EVIDENCE_CHANGED")
    return gate


def _append_archive_legacy(run: dict[str, Any]) -> None:
    if not ARCHIVE_DOC.exists():
        ARCHIVE_DOC.write_text("# rmatest 金标邮件可重复全链路回归测试记录\n\n此文档只保存脱敏结果；原始邮件正文与附件不写入文档。\n", encoding="utf-8")
    lines = [
        "", f"## {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')} — `{run['suite_id']}` / `{run['run_id']}`", "",
        f"- 结果：`{run['status']}`", f"- 用例：`{len(run.get('cases') or [])}`", f"- 系统出站：`{run.get('actual_system_outbound_count', 0)}`", f"- 客户补充：`{run.get('actual_supplement_send_count', 0)}`", f"- SMTP 总数：`{run.get('actual_total_smtp_count', 0)}`", f"- 清理：`{run.get('cleanup_status', 'unknown')}`", "",
        "| Message-ID 哈希 | 结果 | 意图 | 工单状态 | 问题 |", "|---|---|---|---|---|",
    ]
    for case in run.get("cases") or []:
        lines.append(f"| `{case.get('message_id_sha256', '')[:16]}` | `{case.get('status')}` | `{case.get('actual_intent') or '-'}` | `{case.get('actual_status') or '-'}` | {', '.join(case.get('issues') or []) or '-'} |")
    lines.extend(["", "### 本轮问题与下一轮计划", ""])
    issues = sorted({issue for case in run.get("cases") or [] for issue in case.get("issues") or []})
    lines.extend([f"- [ ] {issue}" for issue in issues] or ["- 无新增问题。"])
    with ARCHIVE_DOC.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def _append_archive(run: dict[str, Any]) -> None:
    if not ARCHIVE_DOC.exists():
        ARCHIVE_DOC.write_text(
            "# rmatest 金标邮件可重复全链路回归测试记录\n\n"
            "此文档只保存脱敏结果；原始邮件正文与附件不写入文档。\n",
            encoding="utf-8",
        )
    lines = [
        "",
        f"## {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')} — "
        f"`{run['suite_id']}` / `{run['run_id']}`",
        "",
        f"- 结果：`{run['status']}`",
        f"- 用例：`{len(run.get('cases') or [])}`",
        f"- 系统出站：`{run.get('actual_system_outbound_count', 0)}`",
        f"- 客户补充：`{run.get('actual_supplement_send_count', 0)}`",
        f"- SMTP 总数：`{run.get('actual_total_smtp_count', 0)}`",
        f"- 清理：`{run.get('cleanup_status', 'unknown')}`",
        "",
        "| Message-ID 哈希 | 结果 | 意图 | 工单状态 | 问题 |",
        "|---|---|---|---|---|",
    ]
    for case in run.get("cases") or []:
        lines.append(
            f"| `{case.get('message_id_sha256', '')[:16]}` | `{case.get('status')}` | "
            f"`{case.get('actual_intent') or '-'}` | `{case.get('actual_status') or '-'}` | "
            f"{', '.join(case.get('issues') or []) or '-'} |"
        )
    lines.extend(["", "### 本轮问题与下一轮计划", ""])
    issues = sorted(
        {issue for case in run.get("cases") or [] for issue in case.get("issues") or []}
    )
    lines.extend([f"- [ ] {issue}" for issue in issues] or ["- 无新增问题。"])
    with ARCHIVE_DOC.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def _run_suite_unlocked(
    path: Path,
    confirm_suite: str,
    selected_message_ids: list[str] | None = None,
) -> dict[str, Any]:
    validation = validate_manifest(path, require_approval=True)
    manifest = read_json(path)
    suite_id = str(manifest["suite_id"])
    if confirm_suite != suite_id:
        raise GoldCliError("SUITE_CONFIRMATION_MISMATCH")
    classification_gate = _require_classification_gate(path, manifest)
    health = doctor(live=True)
    if health["status"] != "passed":
        raise GoldCliError("DOCTOR_BLOCKED", details=health)
    run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    run_root = suite_root(suite_id) / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    os.environ.setdefault("INTEGRATION_ADMIN_USERNAME", settings.DEFAULT_ADMIN_USERNAME)
    os.environ.setdefault("INTEGRATION_ADMIN_PASSWORD", settings.DEFAULT_ADMIN_PASSWORD)
    selected = {normalized_message_id(value) for value in selected_message_ids or []}
    messages = [
        item
        for item in manifest["messages"]
        if not selected or str(item["message_id"]) in selected
    ]
    missing_selected = selected - {str(item["message_id"]) for item in messages}
    if missing_selected:
        raise GoldCliError(
            "SELECTED_MESSAGE_ID_NOT_IN_MANIFEST",
            details={
                "message_id_hashes": [
                    hashlib.sha256(value.encode()).hexdigest()
                    for value in sorted(missing_selected)
                ]
            },
        )
    result: dict[str, Any] = {
        "schema_version": 1,
        "suite_id": suite_id,
        "run_id": run_id,
        "started_at": now_iso(),
        "status": "running",
        "cases": [],
        "actual_system_outbound_count": 0,
        "actual_supplement_send_count": 0,
        "actual_total_smtp_count": 0,
        "cleanup_status": "pending",
        "validation": validation,
        "classification_gate": classification_gate,
        "selected_message_count": len(messages),
    }
    client = Client()
    initial: dict[str, Any] | None = None
    all_message_ids = [str(item["message_id"]) for item in messages]
    selected_system_limit = sum(
        int(item["gold"].get("expected_outbound_count") or 0)
        for item in messages
    )
    selected_supplement_limit = sum(
        1
        for item in messages
        if item["gold"].get("send_mode") == "followup_then_rma"
    )
    rmatest2_suite_baseline_uid = 0
    rmatest1_suite_baseline_uid = 0
    supplement_message_ids: set[str] = set()
    try:
        login(client)
        initial = current_config(client)
        _set_and_verify_config(
            client,
            auto_send_enabled=False,
            auto_followup_enabled=False,
            rma_auto_send_enabled=False,
        )
        rmatest2_suite_baseline_uid = _rmatest2_max_uid()
        rmatest1_suite_baseline_uid = _rmatest1_max_uid()
        for index, item in enumerate(messages):
            _refresh_authoritative_send_counts(
                result,
                rmatest2_baseline_uid=rmatest2_suite_baseline_uid,
                rmatest1_baseline_uid=rmatest1_suite_baseline_uid,
                original_message_ids=all_message_ids,
                supplement_message_ids=supplement_message_ids,
            )
            message_id = str(item["message_id"])
            case_root = run_root / f"case-{index + 1:03d}"
            case_root.mkdir(parents=True)
            case: dict[str, Any] = {"message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(), "status": "running", "issues": []}
            result["cases"].append(case)
            mini = _mini_manifest(case_root, manifest, item)
            try:
                raw_uid, raw, _ = _fetch_raw_by_message_id(host=settings.IMAP_HOST, port=settings.IMAP_PORT, user=settings.IMAP_USER, password=settings.IMAP_PASSWORD, folder=settings.IMAP_FOLDER, message_id=message_id)
                del raw_uid
                if hashlib.sha256(raw).hexdigest() != item["raw_sha256"]:
                    raise GoldCliError("SOURCE_EML_HASH_CHANGED")
                run_database_async(_reset([message_id], suite_id=suite_id, run_id=f"{run_id}-pre-{index + 1}", apply=True))
                _relay_reset()
                _relay_control("normal", item["gold"].get("fixed_rma_no"))
                temporary_state = case_root / "temporary-master-state.json"
                run_database_async(
                    apply_temporary_master_data(
                        read_json(mini),
                        temporary_state,
                        allow_gold_e2e_snapshot_override=True,
                    )
                )
                baseline_uid = _rmatest2_max_uid()
                mode = item["gold"]["send_mode"]
                expected_case_outbound = int(item["gold"].get("expected_outbound_count") or 0)
                if (
                    result["actual_system_outbound_count"] + expected_case_outbound
                    > selected_system_limit
                ):
                    raise GoldCliError("SYSTEM_OUTBOUND_HARD_LIMIT_WOULD_BE_EXCEEDED")
                expected_switches = {
                    "auto_send_enabled": mode in {"auto_rma", "followup_then_rma"},
                    "auto_followup_enabled": mode in {"auto_followup", "followup_then_rma"},
                    "rma_auto_send_enabled": mode in {"auto_rma", "followup_then_rma"},
                }
                _set_and_verify_config(client, **expected_switches)
                email_id, fetch_result = _fetch_system_message(
                    client, message_id, expected_switches=expected_switches
                )
                if email_id:
                    parsed_email, parsed_ticket = _wait_for_parse_terminal(
                        client, email_id, expected_switches=expected_switches
                    )
                    del parsed_email
                    if parsed_ticket is not None:
                        _resume_pending_reply_after_enabling(
                            client,
                            email_id=email_id,
                            ticket_detail=parsed_ticket,
                        )
                    initial_status = "auto_replied" if mode == "followup_then_rma" else str(item["gold"].get("expected_final_status") or "")
                    value = _wait_for_case(
                        client,
                        email_id,
                        initial_status,
                        1 if mode == "followup_then_rma" else int(item["gold"].get("expected_outbound_count") or 0),
                        approve_special_policy=mode == "auto_rma",
                        expected_switches=expected_switches,
                    )
                else:
                    value = {
                        "email_detail": {
                            "email": {
                            }
                        },
                        "fetch_result": fetch_result,
                        "ticket_detail": None,
                    }
                if mode == "followup_then_rma":
                    original_email_detail = value.get("email_detail")
                    first = _rmatest2_new_messages(baseline_uid)
                    matching_first = [row for row in first if row.get("in_reply_to") == message_id or message_id in row.get("references", "")]
                    if len(matching_first) != 1:
                        raise GoldCliError("FOLLOWUP_REPLY_NOT_UNIQUE", details={"match_count": len(matching_first)})
                    supplement_id = _send_supplement(
                        message_id,
                        matching_first[0],
                        item["gold"]["supplement"],
                        sent_so_far=result["actual_supplement_send_count"],
                        hard_limit=selected_supplement_limit,
                    )
                    supplement_message_ids.add(supplement_id)
                    result["actual_supplement_send_count"] = len(supplement_message_ids)
                    result["actual_total_smtp_count"] = (
                        result["actual_system_outbound_count"]
                        + result["actual_supplement_send_count"]
                    )
                    supplement_email_id, _ = _fetch_system_message(
                        client,
                        supplement_id,
                        expected_switches=expected_switches,
                    )
                    if not supplement_email_id:
                        raise GoldCliError("SUPPLEMENT_NOT_ARCHIVED")
                    recovered = _wait_for_case(
                        client,
                        supplement_email_id,
                        str(item["gold"]["expected_final_status"]),
                        int(item["gold"]["expected_outbound_count"]),
                        approve_special_policy=True,
                        expected_switches=expected_switches,
                        max_sent_followups=1,
                    )
                    value = {"email_detail": original_email_detail, "ticket_detail": recovered.get("ticket_detail")}
                outbound = _wait_for_case_outbound(
                    baseline_uid,
                    original_message_id=message_id,
                    expected_count=expected_case_outbound,
                    client=client,
                    expected_switches=expected_switches,
                )
                case["issues"] = _assert_case(item, value, outbound)
                email = (value.get("email_detail") or {}).get("email") or {}
                classification = email or value.get("fetch_result") or {}
                ticket = (value.get("ticket_detail") or {}).get("ticket") or {}
                case.update({"actual_intent": classification.get("intent_type"), "actual_status": ticket.get("current_status_code"), "outbound_count": len(outbound), "status": "passed" if not case["issues"] else "failed"})
                result["actual_system_outbound_count"] += len(outbound)
                result["actual_total_smtp_count"] = (
                    result["actual_system_outbound_count"]
                    + result["actual_supplement_send_count"]
                )
            except Exception as exc:
                case["status"] = "error"
                case["issues"] = sorted(
                    set([*case.get("issues", []), _safe_exception_code(exc)])
                )
                details = getattr(exc, "details", None)
                if isinstance(details, dict) and details:
                    case["error_details"] = details
            finally:
                try:
                    _set_and_verify_config(
                        client,
                        auto_send_enabled=False,
                        auto_followup_enabled=False,
                        rma_auto_send_enabled=False,
                    )
                except Exception as exc:
                    case["issues"] = sorted(
                        set(
                            [
                                *case.get("issues", []),
                                f"SWITCH_CLEANUP:{getattr(exc, 'code', type(exc).__name__)}",
                            ]
                        )
                    )
                    case["status"] = "error"
                try:
                    run_database_async(
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
                    run_database_async(_reset([message_id], suite_id=suite_id, run_id=f"{run_id}-post-{index + 1}", apply=True))
                except Exception as exc:
                    case["issues"] = sorted(set([*case.get("issues", []), f"DB_CLEANUP:{getattr(exc, 'code', type(exc).__name__)}"]))
                    case["status"] = "error"
                try:
                    _refresh_authoritative_send_counts(
                        result,
                        rmatest2_baseline_uid=rmatest2_suite_baseline_uid,
                        rmatest1_baseline_uid=rmatest1_suite_baseline_uid,
                        original_message_ids=all_message_ids,
                        supplement_message_ids=supplement_message_ids,
                    )
                except Exception as exc:
                    case["issues"] = sorted(
                        set(
                            [
                                *case.get("issues", []),
                                f"MAILBOX_COUNT:{_safe_exception_code(exc)}",
                            ]
                        )
                    )
                    case["status"] = "error"
        incremental_system_count = result["actual_system_outbound_count"]
        incremental_supplement_count = len(supplement_message_ids)
        observed_system, observed_supplements = _refresh_authoritative_send_counts(
            result,
            rmatest2_baseline_uid=rmatest2_suite_baseline_uid,
            rmatest1_baseline_uid=rmatest1_suite_baseline_uid,
            original_message_ids=all_message_ids,
            supplement_message_ids=supplement_message_ids,
        )
        result["counter_reconciliation"] = {
            "incremental_system": incremental_system_count,
            "mailbox_system": len(observed_system),
            "incremental_supplement": incremental_supplement_count,
            "mailbox_supplement": len(observed_supplements),
        }
        result["counter_reconciliation"]["matched"] = (
            result["counter_reconciliation"]["incremental_system"]
            == len(observed_system)
            and len(supplement_message_ids) == len(observed_supplements)
        )
        mailbox_evidence_dir = run_root / "mailbox-evidence"
        evidence_candidates = _rmatest2_new_messages(
            rmatest2_suite_baseline_uid,
            evidence_dir=mailbox_evidence_dir,
            evidence_thread_message_ids=set(all_message_ids),
        )
        evidence_messages = [
            row
            for row in evidence_candidates
            if TEST_MAIL_SENDER.lower() in str(row.get("from") or "").lower()
            and TEST_MAIL_RECIPIENT.lower() in str(row.get("to") or "").lower()
            and any(
                message_id == row.get("in_reply_to")
                or message_id in str(row.get("references") or "")
                for message_id in all_message_ids
            )
        ]
        supplement_evidence_candidates = _rmatest1_new_messages(
            rmatest1_suite_baseline_uid,
            evidence_dir=mailbox_evidence_dir / "customer-supplements",
            evidence_thread_message_ids=set(supplement_message_ids),
        )
        supplement_evidence_messages = [
            row
            for row in supplement_evidence_candidates
            if row.get("message_id") in supplement_message_ids
        ]
        result["mailbox_evidence"] = {
            "directory": str(mailbox_evidence_dir),
            "message_count": len(evidence_messages),
            "supplement_message_count": len(supplement_evidence_messages),
            "pdf_count": sum(
                1
                for row in evidence_messages
                for attachment in row.get("attachments") or []
                if attachment.get("content_type") == "application/pdf"
            ),
        }
        if result["actual_system_outbound_count"] > selected_system_limit:
            raise GoldCliError("SYSTEM_OUTBOUND_HARD_LIMIT_EXCEEDED")
        if result["actual_supplement_send_count"] > selected_supplement_limit:
            raise GoldCliError("SUPPLEMENT_SEND_HARD_LIMIT_EXCEEDED")
        if result["actual_total_smtp_count"] > selected_system_limit + selected_supplement_limit:
            raise GoldCliError("ACTUAL_SEND_HARD_LIMIT_EXCEEDED")
        result["status"] = "passed" if all(row["status"] == "passed" for row in result["cases"]) else "failed"
        result["previous_run_comparison"] = _compare_with_previous(run_root, result)
    except Exception as exc:
        result["status"] = "error"
        result["fatal_error"] = getattr(exc, "code", type(exc).__name__)
    finally:
        if initial is not None:
            try:
                _set_and_verify_config(
                    client,
                    auto_send_enabled=False,
                    auto_followup_enabled=False,
                    rma_auto_send_enabled=False,
                )
            except Exception as exc:
                result["runtime_restore_error"] = type(exc).__name__
                result["status"] = "error"
        try:
            final_cleanup = run_database_async(_reset(all_message_ids, suite_id=suite_id, run_id=f"{run_id}-final", apply=True))
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


def run_suite(
    path: Path,
    confirm_suite: str,
    selected_message_ids: list[str] | None = None,
) -> dict[str, Any]:
    manifest = read_json(path)
    suite_id = str(manifest.get("suite_id") or "")
    lock_run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-lock")
    with _exclusive_suite_run(suite_id, lock_run_id):
        return _run_suite_unlocked(path, confirm_suite, selected_message_ids)


def report(path: Path) -> dict[str, Any]:
    manifest = read_json(path)
    runs = sorted((suite_root(str(manifest["suite_id"])) / "runs").glob("*/result.json"))
    if not runs:
        raise GoldCliError("NO_RUN_RESULTS")
    latest = read_json(runs[-1])
    return {"status": "available", "archive_document": str(ARCHIVE_DOC), "latest_result": str(runs[-1]), "summary": {"run_status": latest.get("status"), "case_count": len(latest.get("cases") or []), "actual_system_outbound_count": latest.get("actual_system_outbound_count"), "actual_supplement_send_count": latest.get("actual_supplement_send_count"), "actual_total_smtp_count": latest.get("actual_total_smtp_count"), "cleanup_status": latest.get("cleanup_status")}}


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
    egress = sub.add_parser(
        "authorize-egress",
        help="Bind explicit OSS/AI sensitive-data egress authorization to the manifest",
    )
    egress.add_argument("--manifest", type=Path, required=True)
    egress.add_argument("--approved-by", required=True)
    egress.add_argument("--i-authorize-sensitive-egress", action="store_true")
    run = sub.add_parser("run", help="Execute, assert, archive and cleanup all cases")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--confirm-suite", required=True)
    run.add_argument(
        "--message-id",
        action="append",
        help="Run only this exact approved Message-ID; repeat for a controlled subset",
    )
    classify = sub.add_parser("classify", help="Parse the whole suite with all sending disabled")
    classify.add_argument("--manifest", type=Path, required=True)
    classify.add_argument("--confirm-suite", required=True)
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
        elif args.command == "authorize-egress":
            result = authorize_sensitive_egress(
                args.manifest,
                approved_by=args.approved_by,
                acknowledge=args.i_authorize_sensitive_egress,
            )
        elif args.command == "run":
            result = run_suite(args.manifest, args.confirm_suite, args.message_id)
        elif args.command == "classify":
            result = classify_suite(args.manifest, args.confirm_suite)
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
