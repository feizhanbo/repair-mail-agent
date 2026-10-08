from __future__ import annotations

import argparse
import asyncio
import hashlib
import imaplib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterator
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
EVIDENCE_ROOT = PROJECT_ROOT / "test-results" / "prelaunch-full-chain"
REPORT_DOCUMENT = PROJECT_ROOT / "docs" / "全链路回归测试记录.md"
BOUNDARY = datetime(2026, 9, 20, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
EXPECTED_INTENT = "new_repair"
DEFAULT_RELAY_PORT = 18766
EXPECTED_CALL_ID = "567809"


class PrelaunchRegressionError(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _record_key(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("created_at"),
        record.get("trace_id"),
        record.get("call_type"),
        record.get("email_id"),
        record.get("ticket_id"),
        record.get("attachment_id"),
        record.get("mail_fetch_record_id"),
        record.get("status"),
        record.get("error_code"),
    )


def _read_jsonl(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PrelaunchRegressionError(
                "AI_EVIDENCE_JSONL_INVALID", details={"line_no": line_no}
            ) from exc
        if isinstance(value, dict) and value.get("call_type"):
            rows.append(value)
    return rows


def _config_value(name: str, default: str | None = None) -> str | None:
    explicit = os.environ.get(name)
    if explicit is not None:
        return explicit
    from dotenv import dotenv_values

    value = dotenv_values(PROJECT_ROOT / ".env").get(name)
    return str(value) if value not in {None, ""} else default


def _read_remote_ai_jsonl(remote_path: str) -> str:
    import paramiko

    password = _config_value("SSH_PASSWORD")
    if not password:
        raise PrelaunchRegressionError("SSH_PASSWORD_REQUIRED")
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=_config_value("SSH_HOST", "47.100.20.214"),
            port=int(_config_value("SSH_PORT", "22") or "22"),
            username=_config_value("SSH_USER", "root"),
            password=password,
            timeout=10,
            auth_timeout=10,
            banner_timeout=10,
        )
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "r") as stream:
                raw = stream.read()
        finally:
            sftp.close()
    except FileNotFoundError:
        return ""
    finally:
        client.close()
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


def _load_ai_evidence_records(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dates = sorted({_parse_utc(str(row["started_at"])).strftime("%Y/%m/%d") for row in results})
    local_roots = {BACKEND_ROOT / "logs" / "ai"}
    configured_local = _config_value("AI_LOG_DIR")
    if configured_local:
        local_roots.add(Path(configured_local))
    remote_root = str(
        _config_value(
            "REMOTE_AI_LOG_DIR",
            "/root/bert/repair-mail-agent/backend/logs/ai",
        )
    ).rstrip("/")
    records: dict[tuple[Any, ...], dict[str, Any]] = {}
    for date_path in dates:
        file_name = f"ai-{date_path.replace('/', '')}.jsonl"
        for root in local_roots:
            path = root / date_path / file_name
            if path.exists():
                for record in _read_jsonl(path.read_text(encoding="utf-8")):
                    records[_record_key(record)] = record
        remote_path = f"{remote_root}/{date_path}/{file_name}"
        for record in _read_jsonl(_read_remote_ai_jsonl(remote_path)):
            records[_record_key(record)] = record
    return sorted(records.values(), key=lambda item: str(item.get("created_at") or ""))


def _case_attachment_ids(case: dict[str, Any]) -> set[int]:
    return {
        int(row["id"])
        for row in (((case.get("stages") or {}).get("parsing") or {}).get("attachments") or [])
        if row.get("id") is not None
    }


def _record_case_matches(record: dict[str, Any], case: dict[str, Any]) -> bool:
    email_id = case.get("email_id")
    ticket_id = case.get("ticket_id")
    if email_id is not None and record.get("email_id") == email_id:
        return True
    if ticket_id is not None and record.get("ticket_id") == ticket_id:
        return True
    attachment_id = record.get("attachment_id")
    if attachment_id is not None and int(attachment_id) in _case_attachment_ids(case):
        return True
    message_id = str(case.get("message_id") or "")
    if not message_id:
        return False
    searchable = json.dumps(
        {
            "input": record.get("input_payload"),
            "request": record.get("request_payload"),
        },
        ensure_ascii=False,
        default=str,
    )
    return message_id in searchable


def _usage_signature(value: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(value.get("call_type") or ""),
        str(value.get("model") or ""),
        str(value.get("status") or ""),
        str(value.get("error_code") or ""),
    )


def _public_ai_record(record: dict[str, Any]) -> dict[str, Any]:
    from app.services.ai import sanitize_ai_detail

    sanitized = sanitize_ai_detail(record)
    full_keys = {"input_payload", "request_payload", "response_payload", "parsed_result"}
    return {
        "trace_id": sanitized.get("trace_id"),
        "call_type": sanitized.get("call_type"),
        "provider": sanitized.get("provider"),
        "model": sanitized.get("model"),
        "prompt_version": sanitized.get("prompt_version"),
        "prompt_hash": sanitized.get("prompt_hash"),
        "schema_version": sanitized.get("schema_version"),
        "parser_version": sanitized.get("parser_version"),
        "structured_output_method": sanitized.get("structured_output_method"),
        "route_name": sanitized.get("route_name"),
        "route_attempt": sanitized.get("route_attempt"),
        "fallback_used": sanitized.get("fallback_used"),
        "status": sanitized.get("status"),
        "error_code": sanitized.get("error_code"),
        "created_at": sanitized.get("created_at"),
        "tokens": sanitized.get("token_usage"),
        "availability": "full" if full_keys.issubset(sanitized) else "metadata_only",
        "input": sanitized.get("input_payload", sanitized.get("input_metadata")),
        "request": sanitized.get("request_payload", sanitized.get("request_metadata")),
        "response": sanitized.get("response_payload", sanitized.get("response_metadata")),
        "parsed_result": sanitized.get("parsed_result", sanitized.get("parsed_key_result")),
    }


def _enrich_result_with_ai_evidence(
    result: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    started_at = _parse_utc(str(result["started_at"])) - timedelta(seconds=2)
    finished_at = _parse_utc(str(result["finished_at"])) + timedelta(seconds=2)
    window_records = [
        row
        for row in records
        if row.get("created_at")
        and started_at <= _parse_utc(str(row["created_at"])) <= finished_at
    ]
    ambiguous: list[dict[str, Any]] = []
    assignments: dict[str, list[dict[str, Any]]] = {
        str(case.get("message_id")): [] for case in result.get("cases") or []
    }
    for record in window_records:
        matching = [case for case in result.get("cases") or [] if _record_case_matches(record, case)]
        if len(matching) == 1:
            assignments[str(matching[0].get("message_id"))].append(record)
        elif len(matching) > 1:
            ambiguous.append(
                {
                    "trace_id": record.get("trace_id"),
                    "call_type": record.get("call_type"),
                    "created_at": record.get("created_at"),
                    "message_ids": [row.get("message_id") for row in matching],
                }
            )

    missing: list[dict[str, Any]] = []
    metadata_only: list[dict[str, Any]] = []
    captured_count = 0
    expected_count = 0
    for case in result.get("cases") or []:
        message_id = str(case.get("message_id") or "")
        assigned = assignments.get(message_id, [])
        public = [_public_ai_record(row) for row in assigned]
        case["ai_calls"] = public
        captured_count += len(public)
        expected = Counter(_usage_signature(row) for row in case.get("ai_token_usage") or [])
        actual = Counter(_usage_signature(row) for row in assigned)
        expected_count += sum(expected.values())
        for signature, count in (expected - actual).items():
            missing.append(
                {
                    "message_id": message_id,
                    "call_type": signature[0],
                    "model": signature[1],
                    "status": signature[2],
                    "error_code": signature[3] or None,
                    "count": count,
                }
            )
        for row in public:
            if row["availability"] != "full":
                metadata_only.append(
                    {
                        "message_id": message_id,
                        "trace_id": row.get("trace_id"),
                        "call_type": row.get("call_type"),
                    }
                )

    complete = not missing and not ambiguous and not metadata_only and captured_count == expected_count
    result["ai_evidence_summary"] = {
        "status": "complete" if complete else "incomplete",
        "expected_call_count": expected_count,
        "captured_call_count": captured_count,
        "missing_calls": missing,
        "ambiguous_calls": ambiguous,
        "metadata_only_calls": metadata_only,
        "security_policy": {
            "full_text_preserved": True,
            "secret_fields_redacted": True,
            "signed_url_queries_redacted": True,
            "data_urls_and_base64_replaced_with_hash_reference": True,
        },
    }
    return result


def _enrich_batch(batch_id: str) -> dict[str, Any]:
    stability_path = EVIDENCE_ROOT / f"stability-{batch_id}.json"
    if not stability_path.exists():
        raise PrelaunchRegressionError(
            "STABILITY_RESULT_NOT_FOUND", details={"batch_id": batch_id}
        )
    stability = json.loads(stability_path.read_text(encoding="utf-8"))
    result_paths = [EVIDENCE_ROOT / run_id / "result.json" for run_id in stability.get("run_ids") or []]
    missing_paths = [str(path) for path in result_paths if not path.exists()]
    if missing_paths:
        raise PrelaunchRegressionError(
            "PRELAUNCH_RESULTS_NOT_FOUND", details={"paths": missing_paths}
        )
    results = [json.loads(path.read_text(encoding="utf-8")) for path in result_paths]
    records = _load_ai_evidence_records(results)
    summaries: list[dict[str, Any]] = []
    for path, result in zip(result_paths, results, strict=True):
        enriched = _enrich_result_with_ai_evidence(result, records)
        _write_json(path, enriched)
        summaries.append({"run_id": result["run_id"], **enriched["ai_evidence_summary"]})
    complete = bool(summaries) and all(row["status"] == "complete" for row in summaries)
    return {
        "status": "complete" if complete else "incomplete",
        "batch_id": batch_id,
        "runs": summaries,
        "result_paths": [str(path) for path in result_paths],
    }


def _emit(stage: str, value: Any) -> None:
    print(
        json.dumps(
            {"stage": stage, "result": _json_safe(value)},
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _wait_port(port: int, *, opened: bool, timeout: float = 35.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port) is opened:
            return
        time.sleep(0.2)
    raise PrelaunchRegressionError(
        "PORT_STATE_TIMEOUT", details={"port": port, "expected_open": opened}
    )


@contextmanager
def _managed_process(
    command: list[str],
    *,
    ready_port: int,
    log_path: Path,
    require_new: bool = False,
) -> Iterator[None]:
    process: subprocess.Popen[str] | None = None
    stream = None
    if _port_open(ready_port) and require_new:
        raise PrelaunchRegressionError(
            "ISOLATED_PORT_ALREADY_IN_USE", details={"port": ready_port}
        )
    if not _port_open(ready_port):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=BACKEND_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            text=True,
        )
        _wait_port(ready_port, opened=True)
    try:
        yield
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            _wait_port(ready_port, opened=False, timeout=15)
        if stream is not None:
            stream.close()


@contextmanager
def _suite_lock(run_root: Path) -> Iterator[None]:
    import msvcrt

    lock_path = EVIDENCE_ROOT / ".prelaunch-full-chain.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    locked = False
    try:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError as exc:
            raise PrelaunchRegressionError("PRELAUNCH_RUN_ALREADY_ACTIVE") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(str(run_root).encode("utf-8"))
        stream.flush()
        yield
    finally:
        if locked:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()


def _configure_environment(relay_port: int) -> None:
    os.environ["APP_ENV"] = "test"
    os.environ["RELAY_ADAPTER"] = "test_http"
    os.environ["RELAY_SQLSERVER_ENABLED"] = "true"
    os.environ["TEST_RELAY_BASE_URL"] = f"http://127.0.0.1:{relay_port}"
    os.environ["IMAP_INITIAL_SYNC_START_AT"] = BOUNDARY.isoformat()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _imap_inventory() -> list[dict[str, Any]]:
    from app.config import settings

    client = imaplib.IMAP4_SSL(
        settings.IMAP_HOST,
        settings.IMAP_PORT,
        ssl_context=ssl.create_default_context(),
        timeout=30,
    )
    try:
        client.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
        status, _ = client.select(settings.IMAP_FOLDER, readonly=True)
        if status != "OK":
            raise PrelaunchRegressionError("IMAP_FOLDER_SELECT_FAILED")
        status, data = client.uid("SEARCH", None, "SINCE", BOUNDARY.strftime("%d-%b-%Y"))
        if status != "OK":
            raise PrelaunchRegressionError("IMAP_SEARCH_FAILED")
        rows: list[dict[str, Any]] = []
        for uid in (data[0] or b"").split():
            status, fetched = client.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[])")
            metadata = next(
                (part[0] for part in fetched or [] if isinstance(part, tuple)), b""
            )
            raw = next(
                (
                    part[1]
                    for part in fetched or []
                    if isinstance(part, tuple) and isinstance(part[1], bytes)
                ),
                b"",
            )
            if status != "OK" or not raw:
                continue
            import re

            match = re.search(rb'INTERNALDATE\s+"([^"]+)"', metadata)
            internal_date = (
                datetime.strptime(match.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
                if match
                else None
            )
            if internal_date is None or internal_date.astimezone(timezone.utc) < BOUNDARY.astimezone(timezone.utc):
                continue
            message = BytesParser(policy=policy.default).parsebytes(raw)
            attachments = [
                {
                    "file_name": str(part.get_filename() or "attachment"),
                    "content_type": part.get_content_type(),
                    "size_bytes": len(part.get_payload(decode=True) or b""),
                }
                for part in message.walk()
                if part.get_content_disposition() == "attachment" or part.get_filename()
            ]
            rows.append(
                {
                    "uid": uid.decode("ascii"),
                    "message_id": str(message.get("Message-ID") or "").strip(),
                    "subject": str(message.get("Subject") or ""),
                    "from": str(message.get("From") or ""),
                    "to": str(message.get("To") or ""),
                    "internal_date": internal_date.astimezone(BOUNDARY.tzinfo).isoformat(),
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "raw_size_bytes": len(raw),
                    "attachments": attachments,
                }
            )
        return sorted(rows, key=lambda row: int(row["uid"]))
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _relay_control(relay_port: int, token: str) -> dict[str, Any]:
    payload = json.dumps(
        {"scenario": "normal", "delay_seconds": 0, "rma_no": None, "call_id_start": EXPECTED_CALL_ID}
    ).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{relay_port}/control/default",
        data=payload,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


async def _prepare_database(
    message_ids: list[str], *, run_id: str
) -> dict[str, Any]:
    from sqlalchemy import select, text, update

    from app.config import settings
    from app.core.database import AsyncSessionLocal
    from app.models import JobRunLog, MailboxSyncState, MailFetchRecord, User
    from app.services.common import utcnow
    from app.services.gold_replay import (
        apply_gold_test_reset,
        assert_gold_replay_environment,
        plan_gold_test_reset,
        verify_gold_test_reset,
    )

    async with AsyncSessionLocal() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
        environment = await assert_gold_replay_environment(session)
        admin = await session.scalar(
            select(User).where(User.username == settings.DEFAULT_ADMIN_USERNAME)
        )
        if admin is None:
            raise PrelaunchRegressionError("ADMIN_USER_NOT_FOUND")
        plan = await plan_gold_test_reset(session, message_ids=message_ids)
        superseded_job_ids: list[int] = []
        if "GOLD_REPLAY_ACTIVE_JOBS" in plan["blockers"]:
            active_job_ids = [int(value) for value in plan.get("active_job_ids") or []]
            active_jobs = list(
                (
                    await session.execute(
                        select(JobRunLog).where(JobRunLog.id.in_(active_job_ids or [-1]))
                    )
                ).scalars().all()
            )
            running_job_ids = [row.id for row in active_jobs if row.status == "running"]
            if running_job_ids:
                raise PrelaunchRegressionError(
                    "PRELAUNCH_RESET_HAS_RUNNING_TARGET_JOBS",
                    details={"job_ids": running_job_ids},
                )
            superseded_job_ids = [
                row.id for row in active_jobs if row.status in {"queued", "retry_wait"}
            ]
            if superseded_job_ids:
                await session.execute(
                    update(JobRunLog)
                    .where(
                        JobRunLog.id.in_(superseded_job_ids),
                        JobRunLog.status.in_({"queued", "retry_wait"}),
                    )
                    .values(
                        status="superseded",
                        error_code="PRELAUNCH_REPLAY_REPLACED",
                        error_message="Superseded before replaying the same test Message-ID set.",
                        next_run_at=None,
                        locked_at=None,
                        locked_by=None,
                        finished_at=utcnow(),
                    )
                )
                await session.commit()
                plan = await plan_gold_test_reset(session, message_ids=message_ids)
        if plan["blockers"]:
            raise PrelaunchRegressionError(
                "PRELAUNCH_RESET_BLOCKED",
                details={
                    "blockers": plan["blockers"],
                    "active_job_ids": plan.get("active_job_ids") or [],
                },
            )
        reset = await apply_gold_test_reset(
            session,
            message_ids=message_ids,
            expected_plan_hash=plan["plan_hash"],
            suite_id="prelaunch-20260920",
            run_id=run_id,
            user_id=admin.id,
            reason="2026-09-20 上线前全链路回归测试前置清理。",
        )
        verification = await verify_gold_test_reset(session, message_ids=message_ids)
        post_reset_plan = await plan_gold_test_reset(session, message_ids=message_ids)
        cleanup_verified = bool(
            verification.get("verified")
            and post_reset_plan.get("already_clean")
            and not post_reset_plan.get("affected_counts")
            and not post_reset_plan.get("blockers")
            and not reset.get("oss_failed_count")
        )
        if not cleanup_verified:
            raise PrelaunchRegressionError(
                "PRELAUNCH_RESET_VERIFICATION_FAILED",
                details={
                    "verification": verification,
                    "post_reset_plan": post_reset_plan,
                    "oss_failed_count": reset.get("oss_failed_count"),
                },
            )
        active_jobs = list(
            (
                await session.execute(
                    select(JobRunLog).where(
                        JobRunLog.status.in_({"queued", "retry_wait", "running"})
                    )
                )
            ).scalars().all()
        )
        fetch_resource_ids = {
            int(row.resource_id)
            for row in active_jobs
            if row.resource_type == "mail_fetch_record" and row.resource_id is not None
        }
        existing_fetch_resource_ids = set(
            (
                await session.execute(
                    select(MailFetchRecord.id).where(
                        MailFetchRecord.id.in_(fetch_resource_ids or {-1})
                    )
                )
            ).scalars().all()
        )
        superseded_orphan_job_ids = [
            row.id
            for row in active_jobs
            if row.resource_type == "mail_fetch_record"
            and row.resource_id is not None
            and int(row.resource_id) not in existing_fetch_resource_ids
            and row.status in {"queued", "retry_wait"}
        ]
        if superseded_orphan_job_ids:
            await session.execute(
                update(JobRunLog)
                .where(
                    JobRunLog.id.in_(superseded_orphan_job_ids),
                    JobRunLog.status.in_({"queued", "retry_wait"}),
                )
                .values(
                    status="superseded",
                    error_code="PRELAUNCH_ORPHAN_RESOURCE_MISSING",
                    error_message="Referenced mail_fetch_record no longer exists.",
                    next_run_at=None,
                    finished_at=utcnow(),
                )
            )
            await session.commit()
        active_jobs = [row for row in active_jobs if row.id not in superseded_orphan_job_ids]
        if active_jobs:
            raise PrelaunchRegressionError(
                "UNRELATED_ACTIVE_JOBS_PRESENT",
                details={
                    "job_ids": [row.id for row in active_jobs[:50]],
                    "count": len(active_jobs),
                },
            )
        state = await session.scalar(
            select(MailboxSyncState)
            .where(
                MailboxSyncState.mailbox_account == settings.IMAP_USER,
                MailboxSyncState.folder_name == settings.IMAP_FOLDER,
            )
            .with_for_update()
        )
        previous_state = None
        if state is None:
            state = MailboxSyncState(
                mailbox_account=settings.IMAP_USER,
                folder_name=settings.IMAP_FOLDER,
                sync_mode="initializing",
                initial_sync_start_at=BOUNDARY.replace(tzinfo=None),
                version=1,
            )
            session.add(state)
        else:
            previous_state = {
                "sync_mode": state.sync_mode,
                "initial_sync_start_at": state.initial_sync_start_at,
                "last_discovered_uid": state.last_discovered_uid,
                "last_fetched_uid": state.last_fetched_uid,
            }
            state.sync_mode = "rebaseline"
            state.initial_sync_start_at = BOUNDARY.replace(tzinfo=None)
            state.initial_sync_completed_at = None
            state.last_discovered_uid = None
            state.last_fetched_uid = None
            state.last_error_code = None
            state.version = int(state.version or 0) + 1
        await session.commit()
    return {
        "revision": revision,
        "required_revision": "b1c6d7e8f9a0",
        "environment": environment,
        "reset": reset,
        "reset_verification": verification,
        "post_reset_plan": post_reset_plan,
        "cleanup_verified": cleanup_verified,
        "superseded_target_job_ids": superseded_job_ids,
        "superseded_orphan_job_ids": superseded_orphan_job_ids,
        "previous_sync_state": previous_state,
        "boundary": BOUNDARY.isoformat(),
    }


async def _fetch_boundary_mail(limit: int) -> dict[str, Any]:
    from app.config import settings
    from app.core.database import AsyncSessionLocal
    from app.services.imap_fetcher import run_imap_fetch_locked

    async with AsyncSessionLocal() as session:
        result = await run_imap_fetch_locked(
            session,
            folder_name=settings.IMAP_FOLDER,
            limit=limit,
            unseen_only=False,
            auto_parse=True,
            archive_to_oss=True,
            user_id=None,
            busy_is_error=True,
        )
        await session.commit()
        return _json_safe(result)


async def _sent_count_since(started_at: datetime) -> int:
    from sqlalchemy import func, select

    from app.core.database import AsyncSessionLocal
    from app.models import ReplyRecord

    async with AsyncSessionLocal() as session:
        return int(
            await session.scalar(
                select(func.count(ReplyRecord.id)).where(
                    ReplyRecord.send_status == "sent",
                    ReplyRecord.created_at >= started_at,
                )
            )
            or 0
        )


async def _drain_jobs(*, started_at: datetime, max_sends: int) -> list[dict[str, Any]]:
    from sqlalchemy import func, select

    from app.core.database import AsyncSessionLocal
    from app.models import JobRunLog
    from app.integrations.llm_gateway import LlmTask, load_llm_routes
    from app.services.jobs import claim_next_job, execute_claimed_job

    completed: list[dict[str, Any]] = []
    attachment_route = load_llm_routes()[LlmTask.ATTACHMENT_TEXT_PARSE]
    attachment_attempts = (attachment_route.max_retries + 1) * (
        2 if attachment_route.fallback is not None else 1
    )
    drain_timeout = max(
        240,
        int(attachment_route.timeout_seconds * attachment_attempts) + 60,
    )
    deadline = time.monotonic() + drain_timeout
    idle_observations = 0
    for _ in range(600):
        async with AsyncSessionLocal() as claim_session:
            job = await claim_next_job(claim_session, worker_id="prelaunch-full-chain")
            if job is None:
                await claim_session.commit()
                active_count = int(
                    await claim_session.scalar(
                        select(func.count(JobRunLog.id)).where(
                            JobRunLog.created_at >= started_at,
                            JobRunLog.status.in_({"queued", "retry_wait", "running"}),
                        )
                    )
                    or 0
                )
                if active_count == 0:
                    idle_observations += 1
                    if idle_observations >= 2 or time.monotonic() >= deadline:
                        break
                else:
                    idle_observations = 0
                if time.monotonic() >= deadline:
                    raise PrelaunchRegressionError(
                        "JOB_DRAIN_TIMEOUT",
                        details={"active_job_count": active_count},
                    )
                await asyncio.sleep(1)
                continue
            idle_observations = 0
            if job.job_type == "smtp_send" and await _sent_count_since(started_at) >= max_sends:
                await claim_session.rollback()
                raise PrelaunchRegressionError(
                    "SMTP_SEND_HARD_LIMIT_REACHED", details={"max_sends": max_sends}
                )
            job_id = int(job.id)
            await claim_session.commit()
        async with AsyncSessionLocal() as run_session:
            claimed = await run_session.get(JobRunLog, job_id)
            if claimed is None or claimed.status != "running":
                continue
            executed = await execute_claimed_job(run_session, claimed)
            await run_session.commit()
            completed.append(
                {
                    "id": executed.id,
                    "job_type": executed.job_type,
                    "status": executed.status,
                    "error_code": executed.error_code,
                    "result": executed.result_json,
                }
            )
    else:
        raise PrelaunchRegressionError("JOB_DRAIN_LIMIT_EXCEEDED")
    return _json_safe(completed)


async def _jobs_since(started_at: datetime) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models import JobRunLog

    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(JobRunLog)
                    .where(JobRunLog.created_at >= started_at)
                    .order_by(JobRunLog.id)
                )
            ).scalars().all()
        )
    return _json_safe(
        [
            {
                "id": row.id,
                "job_type": row.job_type,
                "status": row.status,
                "error_code": row.error_code,
                "result": row.result_json,
            }
            for row in rows
        ]
    )


async def _poll_simulated_sap(message_ids: list[str]) -> dict[str, Any]:
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models import Email, RepairTicket, TicketRelayExport
    from app.services.sap_rma import poll_export_batch

    async with AsyncSessionLocal() as session:
        export_ids = list(
            (
                await session.execute(
                    select(TicketRelayExport.id)
                    .join(RepairTicket, RepairTicket.id == TicketRelayExport.ticket_id)
                    .join(Email, Email.id == RepairTicket.source_email_id)
                    .where(
                        Email.message_id.in_(message_ids),
                        TicketRelayExport.status.in_({"waiting_sap_result", "waiting_rma"}),
                    )
                    .order_by(TicketRelayExport.id)
                )
            ).scalars().all()
        )
        outcomes = [
            await poll_export_batch(session, export_id=export_id)
            for export_id in export_ids
        ]
        await session.commit()
        return _json_safe(
            {"status": "completed", "export_count": len(export_ids), "results": outcomes}
        )


def _latest_ai_parse(detail: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in detail.get("parse_results") or []
            if row.get("parser_type") == "ai"
        ),
        None,
    )


def _pruned_reply(row: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "reply_type",
        "to_addresses",
        "cc_addresses",
        "subject",
        "review_status",
        "send_status",
        "archive_status",
        "smtp_message_id",
        "smtp_response",
        "rma_pdf_oss_object_id",
        "rma_template_version",
        "sent_at",
        "archive_verified_at",
        "last_error_code",
        "error_message",
    }
    return {key: value for key, value in row.items() if key in allowed}


async def _collect_cases(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from sqlalchemy import or_, select

    from app.core.database import AsyncSessionLocal
    from app.models import AiCallLog, Email, EmailOutbox, MailFetchRecord, ManualReviewTask, RepairTicket
    from app.services.emails import get_email_detail
    from app.services.tickets import get_ticket_detail

    cases: list[dict[str, Any]] = []
    async with AsyncSessionLocal() as session:
        for source in inventory:
            message_id = source["message_id"]
            email = await session.scalar(select(Email).where(Email.message_id == message_id))
            fetch_record = await session.scalar(
                select(MailFetchRecord)
                .where(MailFetchRecord.message_id == message_id)
                .order_by(MailFetchRecord.id.desc())
            )
            if email is None:
                cases.append(
                    {
                        "message_id": message_id,
                        "subject": source["subject"],
                        "issues": ["EMAIL_NOT_INGESTED"],
                        "stages": {
                            "imap": {
                                "status": "failed",
                                "uid": source["uid"],
                                "fetch_status": getattr(fetch_record, "fetch_status", None),
                                "error": getattr(fetch_record, "error_message", None),
                            }
                        },
                    }
                )
                continue
            email_detail = await get_email_detail(session, email.id)
            ai_parse = _latest_ai_parse(email_detail)
            ticket = await session.scalar(
                select(RepairTicket)
                .where(RepairTicket.source_email_id == email.id)
                .order_by(RepairTicket.id.desc())
            )
            ticket_detail = await get_ticket_detail(session, ticket.id) if ticket else None
            ai_calls = list(
                (
                    await session.execute(
                        select(AiCallLog)
                        .where(
                            or_(
                                AiCallLog.email_id == email.id,
                                AiCallLog.mail_fetch_record_id == getattr(fetch_record, "id", None),
                                AiCallLog.ticket_id == (ticket.id if ticket else -1),
                            )
                        )
                        .order_by(AiCallLog.id)
                    )
                ).scalars().all()
            )
            ai_usage = [
                {
                    "call_type": row.call_type,
                    "provider": row.provider_name,
                    "model": row.model_name,
                        "status": row.status,
                        "route_attempt": row.route_attempt,
                        "error_code": row.error_code,
                        "error_message": row.error_message,
                        "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens,
                    "total_tokens": row.total_tokens,
                }
                for row in ai_calls
            ]
            email_tasks = list(
                (
                    await session.execute(
                        select(ManualReviewTask)
                        .where(ManualReviewTask.email_id == email.id)
                        .order_by(ManualReviewTask.id.desc())
                    )
                ).scalars().all()
            )
            outboxes = []
            if ticket is not None:
                rows = list(
                    (
                        await session.execute(
                            select(EmailOutbox)
                            .where(EmailOutbox.ticket_id == ticket.id)
                            .order_by(EmailOutbox.id.desc())
                        )
                    ).scalars().all()
                )
                outboxes = [
                    {
                        "id": row.id,
                        "message_id": row.message_id,
                        "status": row.status,
                        "to_addresses": row.to_addresses,
                        "rma_no": row.rma_no,
                        "request_id": row.request_id,
                        "frozen_eml_sha256": row.frozen_eml_sha256,
                        "pdf_sha256": row.pdf_sha256,
                        "smtp_response": row.smtp_response,
                        "accepted_at": row.accepted_at,
                        "last_error_code": row.last_error_code,
                    }
                    for row in rows
                ]
            issues: list[str] = []
            if email.intent_type != EXPECTED_INTENT:
                issues.append(f"CLASSIFICATION_EXPECTED_{EXPECTED_INTENT.upper()}")
            if ai_parse is None:
                issues.append("AI_PARSE_RESULT_MISSING")
            if ticket is None and not email_tasks:
                issues.append("TICKET_OR_MANUAL_TRACE_MISSING")
            if ticket_detail and ticket.current_status_code == "closed":
                issue_summary = ticket_detail.get("rma_issue_summary") or {}
                for name in (
                    "rma_received",
                    "pdf_validated",
                    "smtp_sent",
                    "message_id_saved",
                    "pdf_archived",
                    "outbound_archived",
                    "closed",
                ):
                    if issue_summary.get(name) is not True:
                        issues.append(f"CLOSURE_EVIDENCE_MISSING:{name}")
                for row in ticket_detail.get("sap_exports") or []:
                    if not row.get("request_id") or not row.get("rma_no") or not row.get("remote_call_id"):
                        issues.append("SAP_SIMULATION_EVIDENCE_INCOMPLETE")
                    if not re.fullmatch(r"\d{6}", str(row.get("remote_call_id") or "")):
                        issues.append("SAP_CALL_ID_FORMAT_INVALID")
                sap_call_ids = {
                    str(row.get("remote_call_id") or "")
                    for row in ticket_detail.get("sap_exports") or []
                }
                if sap_call_ids and EXPECTED_CALL_ID not in sap_call_ids:
                    issues.append("SAP_CALL_ID_567809_MISSING")
            manual_tasks = (
                ticket_detail.get("manual_tasks") if ticket_detail else []
            ) or [
                {
                    "id": row.id,
                    "task_type": row.task_type,
                    "priority": row.priority,
                    "status": row.status,
                    "trigger_reason": row.trigger_reason,
                    "recovery_stage": row.recovery_stage,
                    "recovery_action": row.recovery_action,
                    "created_at": row.created_at,
                }
                for row in email_tasks
            ]
            case = {
                "message_id": message_id,
                "subject": source["subject"],
                "email_id": email.id,
                "ticket_id": ticket.id if ticket else None,
                "ticket_no": ticket.ticket_no if ticket else None,
                "ticket_created": ticket is not None,
                "no_ticket_reason": (
                    None
                    if ticket is not None
                    else next(
                        (str(row.get("trigger_reason")) for row in manual_tasks if row.get("trigger_reason")),
                        "AI_PARSE_RESULT_MISSING" if ai_parse is None else "TICKET_NOT_CREATED",
                    )
                ),
                "ai_token_usage": ai_usage,
                "issues": sorted(set(issues)),
                "stages": {
                    "imap": {
                        "status": "passed",
                        "uid": source["uid"],
                        "internal_date": source["internal_date"],
                        "raw_sha256": source["raw_sha256"],
                        "fetch_status": getattr(fetch_record, "fetch_status", None),
                        "raw_eml_oss_object_id": getattr(fetch_record, "raw_eml_oss_object_id", None),
                    },
                    "classification": {
                        "status": "passed" if email.intent_type == EXPECTED_INTENT else "failed",
                        "intent_type": email.intent_type,
                        "handling_level": email.handling_level,
                        "confidence": email.classification_confidence,
                        "reason_code": email.classification_reason_code,
                        "version": email.classification_version,
                    },
                    "parsing": {
                        "status": "passed" if ai_parse is not None else "failed",
                        "email_parse_status": email.parse_status,
                        "processing_stage": email.processing_stage,
                        "parse_result": {
                            key: (ai_parse or {}).get(key)
                            for key in (
                                "id",
                                "parser_type",
                                "parser_version",
                                "intent_type",
                                "extracted_fields",
                                "extracted_items",
                                "missing_fields",
                                "conflict_fields",
                                "confidence_score",
                                "field_confidences",
                                "apply_status",
                                "error_message",
                            )
                        },
                        "attachments": [
                            {
                                key: row.get(key)
                                for key in (
                                    "id",
                                    "file_name",
                                    "content_type",
                                    "file_size",
                                    "parse_status",
                                    "parse_error",
                                    "extracted_json",
                                )
                            }
                            for row in email_detail.get("attachments") or []
                        ],
                    },
                    "matching": {
                        "status": "passed" if ticket_detail else "manual_required",
                        "ticket": (
                            {
                                key: ticket_detail["ticket"].get(key)
                                for key in (
                                    "id",
                                    "ticket_no",
                                    "customer_code",
                                    "customer_name",
                                    "customer_scope",
                                    "assigned_user_id",
                                    "service_policy_id",
                                    "policy_resolution_status",
                                    "charge_status",
                                    "current_status_code",
                                )
                            }
                            if ticket_detail
                            else None
                        ),
                        "items": (ticket_detail or {}).get("items") or [],
                    },
                    "validation": {
                        "status": (
                            "passed"
                            if ticket and ticket.sn_validation_status == "passed" and ticket.safety_check_hash
                            else ("manual_required" if manual_tasks else "not_reached")
                        ),
                        "sn_validation_status": getattr(ticket, "sn_validation_status", None),
                        "sn_validation_snapshot": getattr(ticket, "sn_validation_snapshot", None),
                        "safety_check_snapshot": getattr(ticket, "safety_check_snapshot", None),
                        "results": (ticket_detail or {}).get("sn_validation_results") or [],
                    },
                    "sap_simulation": {
                        "status": (
                            "passed"
                            if (ticket_detail or {}).get("sap_exports")
                            and all(
                                row.get("rma_no") and row.get("remote_call_id")
                                for row in ticket_detail.get("sap_exports") or []
                            )
                            else ("manual_required" if manual_tasks else "not_reached")
                        ),
                        "adapter": "test_http",
                        "simulated": True,
                        "summary": (ticket_detail or {}).get("sap_export_summary"),
                        "exports": (ticket_detail or {}).get("sap_exports") or [],
                        "rma_records": (ticket_detail or {}).get("rma_records") or [],
                    },
                    "outbound_email": {
                        "status": (
                            "passed"
                            if any(
                                row.get("reply_type") == "rma_authorization"
                                and row.get("send_status") == "sent"
                                for row in (ticket_detail or {}).get("reply_records") or []
                            )
                            else ("manual_required" if manual_tasks else "not_reached")
                        ),
                        "replies": [
                            _pruned_reply(row)
                            for row in (ticket_detail or {}).get("reply_records") or []
                        ],
                        "outboxes": outboxes,
                    },
                    "closure": {
                        "status": (
                            "passed"
                            if ticket and ticket.current_status_code == "closed"
                            else ("manual_required" if manual_tasks else "not_closed")
                        ),
                        "ticket_status": getattr(ticket, "current_status_code", None),
                        "closed_at": getattr(ticket, "closed_at", None),
                        "terminal_reason_code": getattr(ticket, "terminal_reason_code", None),
                        "rma_issue_summary": (ticket_detail or {}).get("rma_issue_summary"),
                        "status_logs": (ticket_detail or {}).get("status_logs") or [],
                    },
                    "manual_intervention": {
                        "status": "recorded" if manual_tasks else "not_required",
                        "tasks": manual_tasks,
                    },
                },
            }
            cases.append(_json_safe(case))
    return cases


def _token_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    by_task: dict[str, dict[str, Any]] = {}
    for case in cases:
        for call in case.get("ai_token_usage") or []:
            key = f"{call.get('call_type')}|{call.get('model')}"
            bucket = by_task.setdefault(
                key,
                {
                    "call_type": call.get("call_type"),
                    "model": call.get("model"),
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "usage_missing_count": 0,
                },
            )
            bucket["calls"] += 1
            if call.get("total_tokens") is None:
                bucket["usage_missing_count"] += 1
            for name in ("input_tokens", "output_tokens", "total_tokens"):
                bucket[name] += int(call.get(name) or 0)
    rows = sorted(by_task.values(), key=lambda row: (str(row["call_type"]), str(row["model"])))
    return {
        "by_task_and_model": rows,
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "total_tokens": sum(row["total_tokens"] for row in rows),
        "usage_missing_count": sum(row["usage_missing_count"] for row in rows),
    }


def _document_section(result: dict[str, Any]) -> str:
    summary = result["summary"]
    token_summary = result.get("token_usage") or {}
    lines = [
        f"## {result['run_id']} 上线前全链路真实回归",
        "",
        f"- 总体结果：`{result['status']}`",
        f"- IMAP 边界：`{result['boundary']}`",
        f"- 入站邮件：{summary['mail_count']} 封",
        f"- 闭合工单：{summary['closed_count']} 个",
        f"- 人工介入：{summary['manual_case_count']} 个场景",
        f"- 真实 SMTP：{summary['smtp_sent_count']} 封（硬上限 {result['max_sends']}）",
        f"- Token：输入 {token_summary.get('input_tokens', 0)} / 输出 {token_summary.get('output_tokens', 0)} / 合计 {token_summary.get('total_tokens', 0)}；usage 缺失 {token_summary.get('usage_missing_count', 0)} 次",
        "- SAP：本地 Test Relay 模拟；每行保留 RequestID、RMA 编号和 CallID，未访问真实 SQL Server。",
        f"- 完整机器证据：`../test-results/prelaunch-full-chain/{result['run_id']}/result.json`",
        "",
        "### 环节汇总",
        "",
        "| Message-ID | 分类 | 解析 | 匹配/校验 | SAP 模拟 | 邮件 | 闭单 | 人工痕迹 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in result["cases"]:
        stages = case["stages"]
        classification = stages.get("classification") or {}
        parsing = stages.get("parsing") or {}
        matching = stages.get("matching") or {}
        validation = stages.get("validation") or {}
        sap = stages.get("sap_simulation") or {}
        outbound = stages.get("outbound_email") or {}
        closure = stages.get("closure") or {}
        manual = stages.get("manual_intervention") or {}
        lines.append(
            "| `{}` | {} / {} | {} | {} / {} | {} | {} | {} | {} |".format(
                case["message_id"],
                classification.get("intent_type"), classification.get("status", "not_reached"),
                parsing.get("status", "not_reached"), matching.get("status", "not_reached"),
                validation.get("status", "not_reached"), sap.get("status", "not_reached"),
                outbound.get("status", "not_reached"), closure.get("status", "not_reached"),
                manual.get("status", "not_required"),
            )
        )
    lines.extend(["", "### 逐封概述", ""])
    for index, case in enumerate(result["cases"], start=1):
        stages = case.get("stages") or {}
        parse_result = ((stages.get("parsing") or {}).get("parse_result") or {})
        fields = sorted((parse_result.get("extracted_fields") or {}).keys())
        missing = sorted((parse_result.get("missing_fields") or {}).keys())
        conflicts = sorted((parse_result.get("conflict_fields") or {}).keys())
        manual_reasons = sorted({
            str(row.get("trigger_reason"))
            for row in ((stages.get("manual_intervention") or {}).get("tasks") or [])
            if row.get("trigger_reason")
        })
        case_tokens = sum(int(row.get("total_tokens") or 0) for row in case.get("ai_token_usage") or [])
        lines.extend(
            [
                f"#### {index}. {case['subject']}",
                "",
                f"- Message-ID：`{case['message_id']}`",
                f"- 工单：`{case.get('ticket_no') or '未建单'}`",
                f"- 未建单原因：{case.get('no_ticket_reason') or '不适用'}",
                f"- 解析字段：{('、'.join(fields) if fields else '无')}",
                f"- 缺失字段：{('、'.join(missing) if missing else '无')}；冲突字段：{('、'.join(conflicts) if conflicts else '无')}",
                f"- 人工原因：{('、'.join(manual_reasons) if manual_reasons else '无')}",
                f"- Token 合计：{case_tokens}",
                f"- 问题：{('、'.join(case['issues']) if case['issues'] else '无')} ",
                "",
            ]
        )
    lines.extend(["### 上线结论", ""])
    lines.extend(
        [f"- [ ] {issue}" for issue in result.get("issues") or []]
        or ["- 本轮未发现未解决的上线阻断项。"]
    )
    return "\n".join(lines) + "\n"


def _rebuild_document() -> None:
    REPORT_DOCUMENT.parent.mkdir(parents=True, exist_ok=True)
    sections = [
        "# 全链路回归测试记录\n\n"
        "> 概述真实 IMAP/AI/OSS/MySQL/SMTP 链路与本地模拟 SAP 中转；详细机器证据见各轮 result.json。\n\n"
    ]
    for path in sorted(EVIDENCE_ROOT.glob("*/result.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        sections.append(_document_section(value))
    REPORT_DOCUMENT.write_text("".join(sections), encoding="utf-8")


async def _execute_run(
    *,
    run_id: str,
    run_root: Path,
    relay_port: int,
    max_sends: int,
    recipient_baseline_uid: int,
) -> dict[str, Any]:
    from app.config import settings
    from app.integrations.llm_gateway import public_llm_routes
    from app.services.mail_safety import test_mail_configuration_reasons
    from app.services.runtime_config import load_runtime_config
    from app.core.database import AsyncSessionLocal
    from app.services.common import utcnow
    from tools.run_gold_mail_regression import _rmatest2_new_messages

    async with AsyncSessionLocal() as session:
        await load_runtime_config(session)
        await session.commit()
    settings.IMAP_INITIAL_SYNC_START_AT = BOUNDARY.replace(tzinfo=None)
    settings.RELAY_ADAPTER = "test_http"
    settings.RELAY_SQLSERVER_ENABLED = True
    settings.TEST_RELAY_BASE_URL = f"http://127.0.0.1:{relay_port}"
    settings.AUTO_SEND_ENABLED = True
    settings.AUTO_FOLLOWUP_ENABLED = False
    reasons = test_mail_configuration_reasons()
    if reasons:
        raise PrelaunchRegressionError("MAIL_SAFETY_GATE_FAILED", details={"reasons": reasons})

    inventory = _imap_inventory()
    if not inventory:
        raise PrelaunchRegressionError("NO_MAIL_AFTER_BOUNDARY")
    if len(inventory) > max_sends:
        raise PrelaunchRegressionError(
            "MAIL_COUNT_EXCEEDS_SMTP_HARD_LIMIT",
            details={"mail_count": len(inventory), "max_sends": max_sends},
        )
    if any(not row["message_id"] for row in inventory):
        raise PrelaunchRegressionError("INVENTORY_MESSAGE_ID_MISSING")
    unexpected_envelopes = [
        row["message_id"]
        for row in inventory
        if [address.lower() for _, address in getaddresses([row["from"]]) if address]
        != ["rmatest2@accotest.com"]
        or "rmatest1@accotest.com"
        not in [address.lower() for _, address in getaddresses([row["to"]]) if address]
    ]
    if unexpected_envelopes:
        raise PrelaunchRegressionError(
            "UNEXPECTED_TEST_MAIL_ENVELOPE",
            details={"message_ids": unexpected_envelopes},
        )
    _write_json(run_root / "inventory.json", inventory)
    _emit("imap_inventory", inventory)

    database = await _prepare_database(
        [row["message_id"] for row in inventory], run_id=run_id
    )
    _emit("database_and_sync_preparation", database)

    run_started_at = utcnow()
    fetch = await _fetch_boundary_mail(max(50, len(inventory)))
    _emit("imap_fetch", fetch)

    jobs: list[dict[str, Any]] = []
    jobs.extend(await _drain_jobs(started_at=run_started_at, max_sends=max_sends))
    _emit("classification_parse_match_validate_and_relay_jobs", jobs)

    sap_poll = await _poll_simulated_sap([row["message_id"] for row in inventory])
    _emit("sap_simulated_rma_and_callid", sap_poll)
    jobs.extend(await _drain_jobs(started_at=run_started_at, max_sends=max_sends))
    jobs = await _jobs_since(run_started_at)
    _emit("rma_smtp_archive_jobs", jobs)

    cases = await _collect_cases(inventory)
    sent_replies = [
        reply
        for case in cases
        for reply in case["stages"]["outbound_email"].get("replies") or []
        if reply.get("send_status") == "sent"
    ]
    if len(sent_replies) > max_sends:
        raise PrelaunchRegressionError(
            "SMTP_SEND_HARD_LIMIT_EXCEEDED",
            details={"actual": len(sent_replies), "max_sends": max_sends},
        )
    expected_message_ids = {
        reply.get("smtp_message_id") for reply in sent_replies if reply.get("smtp_message_id")
    }
    outbound_mailbox: list[dict[str, Any]] = []
    deadline = time.monotonic() + 90
    while True:
        outbound_mailbox = _rmatest2_new_messages(
            recipient_baseline_uid,
            evidence_dir=run_root / "recipient-mailbox-evidence",
            evidence_thread_message_ids={row["message_id"] for row in inventory},
        )
        observed = {row.get("message_id") for row in outbound_mailbox}
        if expected_message_ids.issubset(observed) or time.monotonic() >= deadline:
            break
        await asyncio.sleep(2)
    mailbox_summary = {
        "baseline_uid": recipient_baseline_uid,
        "expected_message_ids": sorted(expected_message_ids),
        "observed_message_ids": sorted(
            row.get("message_id") for row in outbound_mailbox if row.get("message_id")
        ),
        "matched": expected_message_ids.issubset(
            {row.get("message_id") for row in outbound_mailbox}
        ),
        "messages": [
            {key: row.get(key) for key in ("uid", "message_id", "subject", "from", "to", "cc", "in_reply_to", "references", "attachments")}
            for row in outbound_mailbox
        ],
    }
    _emit("recipient_mailbox_verification", mailbox_summary)

    issues = sorted(
        {issue for case in cases for issue in case.get("issues") or []}
    )
    closed_count = sum(
        1 for case in cases if case["stages"]["closure"]["status"] == "passed"
    )
    if closed_count == 0:
        issues.append("NO_TICKET_COMPLETED_FULL_CHAIN")
    if expected_message_ids and not mailbox_summary["matched"]:
        issues.append("RECIPIENT_MAILBOX_EVIDENCE_MISSING")
    summary = {
        "mail_count": len(inventory),
        "case_count": len(cases),
        "closed_count": closed_count,
        "manual_case_count": sum(
            1
            for case in cases
            if case["stages"]["manual_intervention"]["status"] == "recorded"
        ),
        "smtp_sent_count": len(sent_replies),
        "failed_case_count": sum(1 for case in cases if case.get("issues")),
    }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "passed" if not issues else "failed",
        "boundary": BOUNDARY.isoformat(),
        "max_sends": max_sends,
        "started_at": run_started_at.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
        "database": database,
        "llm_routes": public_llm_routes(),
        "imap_fetch": fetch,
        "sap_poll": sap_poll,
        "jobs": jobs,
        "recipient_mailbox": mailbox_summary,
        "summary": summary,
        "issues": sorted(set(issues)),
        "cases": cases,
        "token_usage": _token_summary(cases),
    }


def _run_once(args: argparse.Namespace, *, batch_id: str, run_index: int) -> dict[str, Any]:
    if not args.confirm_real_smtp:
        raise PrelaunchRegressionError("REAL_SMTP_CONFIRMATION_REQUIRED")
    if args.max_sends < 1 or args.max_sends > 5:
        raise PrelaunchRegressionError("MAX_SENDS_MUST_BE_1_TO_5")
    _configure_environment(args.relay_port)
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))
    from app.config import settings
    from tools.run_gold_mail_regression import _rmatest2_max_uid

    token = str(settings.TEST_RELAY_TOKEN or "")
    if len(token) < 24:
        raise PrelaunchRegressionError("TEST_RELAY_TOKEN_REQUIRED")
    run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    run_root = EVIDENCE_ROOT / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    relay_db = run_root / "test-relay.sqlite3"
    tunnel_command = [sys.executable, "-m", "tools.run_mysql_ssh_tunnel"]
    relay_command = [
        sys.executable,
        "-m",
        "tools.test_relay_server",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.relay_port),
        "--database",
        str(relay_db),
        "--token",
        token,
    ]
    result: dict[str, Any]
    with _suite_lock(run_root), ExitStack() as stack:
        stack.enter_context(
            _managed_process(
                tunnel_command,
                ready_port=13307,
                log_path=run_root / "mysql-tunnel.log",
            )
        )
        stack.enter_context(
            _managed_process(
                relay_command,
                ready_port=args.relay_port,
                log_path=run_root / "test-relay.log",
                require_new=True,
            )
        )
        relay = _relay_control(args.relay_port, token)
        _emit("sap_test_relay", relay)
        recipient_baseline_uid = _rmatest2_max_uid()
        try:
            result = args._event_loop.run_until_complete(
                _execute_run(
                    run_id=run_id,
                    run_root=run_root,
                    relay_port=args.relay_port,
                    max_sends=args.max_sends,
                    recipient_baseline_uid=recipient_baseline_uid,
                )
            )
        except Exception as exc:
            result = {
                "schema_version": 1,
                "run_id": run_id,
                "status": "error",
                "boundary": BOUNDARY.isoformat(),
                "max_sends": args.max_sends,
                "finished_at": datetime.now().astimezone().isoformat(),
                "fatal_error": getattr(exc, "code", type(exc).__name__),
                "fatal_details": getattr(exc, "details", {}),
                "fatal_message": str(exc)[:2000],
                "summary": {
                    "mail_count": 0,
                    "case_count": 0,
                    "closed_count": 0,
                    "manual_case_count": 0,
                    "smtp_sent_count": 0,
                    "failed_case_count": 0,
                },
                "issues": [getattr(exc, "code", type(exc).__name__)],
                "cases": [],
                "token_usage": _token_summary([]),
            }
        finally:
            # Dispose while the SSH tunnel and its event loop are still alive;
            # otherwise the next repeated run can inherit dead pooled sockets.
            from app.core.database import engine

            args._event_loop.run_until_complete(engine.dispose())
    result["batch_id"] = batch_id
    result["run_index"] = run_index
    _write_json(run_root / "result.json", result)
    return result


def _business_snapshot(case: dict[str, Any]) -> dict[str, Any]:
    stages = case.get("stages") or {}
    parsing = stages.get("parsing") or {}
    parse_result = parsing.get("parse_result") or {}
    return {
        "classification": {
            key: (stages.get("classification") or {}).get(key)
            for key in ("status", "intent_type", "handling_strategy")
        },
        "parsing": {
            "status": parsing.get("status"),
            "extracted_fields": parse_result.get("extracted_fields") or {},
            "extracted_items": parse_result.get("extracted_items") or [],
            "missing_fields": parse_result.get("missing_fields") or {},
            "conflict_fields": parse_result.get("conflict_fields") or {},
        },
        "ticket_created": bool(case.get("ticket_created")),
        "no_ticket_reason": case.get("no_ticket_reason"),
        "manual_reasons": sorted(
            str(row.get("trigger_reason"))
            for row in ((stages.get("manual_intervention") or {}).get("tasks") or [])
            if row.get("trigger_reason")
        ),
        "closure": {
            key: (stages.get("closure") or {}).get(key)
            for key in ("status", "ticket_status", "terminal_reason_code")
        },
    }


def _stability_report(batch_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    by_message: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        for case in result.get("cases") or []:
            by_message.setdefault(case["message_id"], []).append(
                {"run_id": result["run_id"], "snapshot": _business_snapshot(case)}
            )
    messages = []
    for message_id, observations in sorted(by_message.items()):
        snapshots = [json.dumps(row["snapshot"], ensure_ascii=False, sort_keys=True, default=str) for row in observations]
        messages.append({
            "message_id": message_id,
            "observations": observations,
            "stable": len(observations) == len(results) and len(set(snapshots)) == 1,
        })
    batch_token_usage = _token_summary(
        [case for result in results for case in (result.get("cases") or [])]
    )
    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "run_ids": [row["run_id"] for row in results],
        "run_statuses": [row["status"] for row in results],
        "stable": bool(messages) and all(row["stable"] for row in messages),
        "messages": messages,
        "token_usage": batch_token_usage,
    }


def _append_stability_document(report: dict[str, Any], path: Path) -> None:
    lines = [
        f"## 批次 {report['batch_id']} 三轮稳定性",
        "",
        f"- 业务输出稳定：`{report['stable']}`",
        f"- 三轮状态：{'、'.join(report['run_statuses'])}",
        f"- Token 合计：{report['token_usage']['total_tokens']}（usage 缺失 {report['token_usage']['usage_missing_count']} 次）",
        f"- 稳定性详细证据：`../test-results/prelaunch-full-chain/{path.name}`",
        "",
        "| Message-ID | 三轮均出现 | 分类/解析/走向稳定 |",
        "| --- | --- | --- |",
    ]
    for row in report["messages"]:
        lines.append(f"| `{row['message_id']}` | {len(row['observations']) == 3} | {row['stable']} |")
    with REPORT_DOCUMENT.open("a", encoding="utf-8") as stream:
        stream.write("\n" + "\n".join(lines) + "\n")


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.repeat < 1 or args.repeat > 5:
        raise PrelaunchRegressionError("REPEAT_MUST_BE_1_TO_5")
    batch_id = datetime.now().astimezone().strftime("batch-%Y%m%d-%H%M%S-%f")
    event_loop = asyncio.new_event_loop()
    args._event_loop = event_loop
    try:
        results = [
            _run_once(args, batch_id=batch_id, run_index=index)
            for index in range(1, args.repeat + 1)
        ]
    finally:
        event_loop.close()
    report = _stability_report(batch_id, results)
    stability_path = EVIDENCE_ROOT / f"stability-{batch_id}.json"
    _write_json(stability_path, report)
    _rebuild_document()
    _append_stability_document(report, stability_path)
    return {
        "status": "passed" if all(row["status"] == "passed" for row in results) and report["stable"] else "failed",
        "batch_id": batch_id,
        "run_ids": report["run_ids"],
        "run_statuses": report["run_statuses"],
        "stable": report["stable"],
        "token_usage": report["token_usage"],
        "stability_result": str(stability_path),
        "document": str(REPORT_DOCUMENT),
    }


def _latest_report() -> dict[str, Any]:
    results = sorted(EVIDENCE_ROOT.glob("*/result.json"))
    if not results:
        raise PrelaunchRegressionError("NO_PRELAUNCH_RESULT")
    value = json.loads(results[-1].read_text(encoding="utf-8"))
    return {
        "status": "available",
        "result": str(results[-1]),
        "document": str(REPORT_DOCUMENT),
        "summary": value.get("summary"),
        "run_status": value.get("status"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="2026-09-20 prelaunch real-business full-chain regression"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--confirm-real-smtp", action="store_true")
    run.add_argument("--max-sends", type=int, default=5)
    run.add_argument("--relay-port", type=int, default=DEFAULT_RELAY_PORT)
    run.add_argument("--repeat", type=int, default=1)
    sub.add_parser("inventory")
    sub.add_parser("report")
    enrich = sub.add_parser("enrich-ai")
    enrich.add_argument("--batch-id", required=True)
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args()
    try:
        if args.command == "run":
            result = _run(args)
        elif args.command == "inventory":
            _configure_environment(DEFAULT_RELAY_PORT)
            if str(BACKEND_ROOT) not in sys.path:
                sys.path.insert(0, str(BACKEND_ROOT))
            result = {
                "status": "available",
                "boundary": BOUNDARY.isoformat(),
                "messages": _imap_inventory(),
            }
        elif args.command == "enrich-ai":
            result = _enrich_batch(args.batch_id)
        else:
            result = _latest_report()
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2, default=str))
        if result.get("status") in {"failed", "error", "blocked"}:
            raise SystemExit(2)
    except PrelaunchRegressionError as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": exc.code, "details": exc.details}},
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
