from __future__ import annotations

import json
import os
import sys
import time
from email.utils import getaddresses
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from app.services.business_rules import CUSTOMER_REQUIRED_FIELD_SET


BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TEST_SENDER = "rmatest1@accotest.com"
TEST_RECIPIENT = "rmatest2@accotest.com"
TERMINAL_JOB_STATUSES = {"success", "needs_manual_review", "failed", "cancelled"}
POLL_SECONDS = 2
TIMEOUT_SECONDS = max(
    10,
    min(300, int(os.getenv("E2E_WAIT_TIMEOUT_SECONDS", "300"))),
)


class E2EFailure(RuntimeError):
    pass


class Client:
    def __init__(self) -> None:
        self.token: str | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Correlation-ID": f"new-repair-e2e-{uuid4().hex}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(
            url,
            method=method,
            headers=headers,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
        except HTTPError as exc:
            payload = exc.read()
            detail = payload.decode("utf-8", errors="replace")
            raise E2EFailure(f"{method} {path} failed with HTTP {exc.code}: {detail[:1000]}") from exc
        except URLError as exc:
            raise E2EFailure(f"{method} {path} could not connect to the backend") from exc
        return json.loads(payload.decode("utf-8")) if payload else {}

    def data(self, method: str, path: str, **kwargs: Any) -> Any:
        payload = self.request(method, path, **kwargs)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise E2EFailure(f"{method} {path} returned an unsuccessful response")
        return payload.get("data")


def require_environment() -> tuple[str, str]:
    if os.getenv("RUN_REAL_MAIL_INTEGRATION_TESTS") != "1":
        raise E2EFailure("RUN_REAL_MAIL_INTEGRATION_TESTS must equal 1")
    complete_message_id = os.getenv("E2E_COMPLETE_MESSAGE_ID", "").strip()
    missing_message_id = os.getenv("E2E_MISSING_MESSAGE_ID", "").strip()
    if not complete_message_id or not missing_message_id:
        raise E2EFailure("Both E2E_COMPLETE_MESSAGE_ID and E2E_MISSING_MESSAGE_ID are required")
    if complete_message_id == missing_message_id:
        raise E2EFailure("The two E2E Message-IDs must be different")
    if not os.getenv("INTEGRATION_ADMIN_USERNAME") or not os.getenv("INTEGRATION_ADMIN_PASSWORD"):
        raise E2EFailure("Integration admin credentials are required as process environment variables")
    return complete_message_id, missing_message_id


def login(client: Client) -> None:
    data = client.data(
        "POST",
        "/api/v1/auth/login",
        body={
            "username": os.environ["INTEGRATION_ADMIN_USERNAME"],
            "password": os.environ["INTEGRATION_ADMIN_PASSWORD"],
        },
    )
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise E2EFailure("Login response did not contain an access token")
    client.token = str(token)


def wait_until(label: str, loader: Callable[[], Any], predicate: Callable[[Any], bool]) -> Any:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_value: Any = None
    while time.monotonic() < deadline:
        last_value = loader()
        if predicate(last_value):
            return last_value
        time.sleep(POLL_SECONDS)
    raise E2EFailure(f"Timed out waiting for {label}; last state={last_value!r}")


def assert_database_preflight(preflight: dict[str, Any]) -> None:
    if preflight.get("status") != "passed" or preflight.get("messages_sent") != 0:
        raise E2EFailure(f"Mail preflight did not pass: {preflight.get('reasons')}")
    database = preflight.get("database") or {}
    if database.get("status") != "ready" or database.get("current_revision") != database.get("required_revision"):
        raise E2EFailure("Database revision is not current")
    smtp = preflight.get("smtp") or {}
    if smtp.get("stage") != "complete" or smtp.get("messages_sent") != 0:
        raise E2EFailure("SMTP login/NOOP preflight did not complete safely")


def find_email(client: Client, message_id: str) -> dict[str, Any] | None:
    page = client.data(
        "GET",
        "/api/v1/emails",
        params={"message_id": message_id, "page": 1, "page_size": 10},
    )
    items = page.get("items", []) if isinstance(page, dict) else []
    exact = [item for item in items if item.get("message_id") == message_id]
    if len(exact) > 1:
        raise E2EFailure(f"Message-ID {message_id} matched more than one archived email")
    return exact[0] if exact else None


def wait_for_job(client: Client, job_id: int) -> dict[str, Any]:
    job = wait_until(
        f"job {job_id}",
        lambda: client.data("GET", f"/api/v1/jobs/{job_id}"),
        lambda value: value.get("status") in TERMINAL_JOB_STATUSES,
    )
    if job.get("status") != "success":
        raise E2EFailure(f"Job {job_id} ended as {job.get('status')} with error {job.get('error_code')}")
    return job


def fetch_exact_message(client: Client, message_id: str, *, expect_duplicate: bool = False) -> int:
    response = client.data(
        "POST",
        "/api/v1/emails/fetch/jobs",
        params={
            "folder_name": "INBOX",
            "limit": 1,
            "unseen_only": "false",
            "message_id": message_id,
            "auto_parse": "true",
        },
    )
    job_payload = response.get("job") if isinstance(response, dict) else None
    if not isinstance(job_payload, dict) or not job_payload.get("id"):
        raise E2EFailure("IMAP fetch did not return a job")
    if response.get("reused"):
        raise E2EFailure("An unrelated active IMAP job was reused; wait for the queue to become idle")
    job = wait_for_job(client, int(job_payload["id"]))
    result = job.get("result_json") or {}
    fetched = result.get("fetched", []) if isinstance(result, dict) else []
    matches = [item for item in fetched if item.get("message_id") == message_id]
    if expect_duplicate and not matches and len(fetched) == 1:
        candidate = fetched[0]
        if candidate.get("fetch_status") == "duplicate_uid_skipped":
            matches = [candidate]
    if len(matches) != 1:
        raise E2EFailure(f"IMAP job did not return exactly one result for {message_id}")
    item = matches[0]
    duplicate = bool(item.get("duplicate")) or item.get("fetch_status") in {
        "duplicate_message_skipped",
        "already_processed_uid",
        "duplicate_uid_skipped",
    }
    if duplicate != expect_duplicate:
        raise E2EFailure(f"Unexpected duplicate result for {message_id}: {item.get('fetch_status')}")
    email_id = item.get("email_id")
    if not email_id:
        existing = find_email(client, message_id)
        email_id = existing.get("id") if existing else None
    if not email_id:
        raise E2EFailure(f"No email_id was recorded for {message_id}")
    return int(email_id)


def wait_for_ticket(client: Client, email_id: int, *, expected_status: str, expected_reply_type: str) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        email_detail = client.data("GET", f"/api/v1/emails/{email_id}")
        parse_results = email_detail.get("parse_results", [])
        ticket_ids = [result.get("ticket_id") for result in parse_results if result.get("ticket_id")]
        if not ticket_ids:
            return {"email_detail": email_detail, "ticket_detail": None}
        ticket_detail = client.data("GET", f"/api/v1/tickets/{int(ticket_ids[0])}")
        return {"email_detail": email_detail, "ticket_detail": ticket_detail}

    def ready(value: dict[str, Any]) -> bool:
        detail = value.get("ticket_detail") or {}
        ticket = detail.get("ticket") or {}
        replies = detail.get("reply_records") or []
        return ticket.get("current_status_code") == expected_status and any(
            reply.get("reply_type") == expected_reply_type and reply.get("send_status") == "sent"
            for reply in replies
        )

    return wait_until(f"ticket for email {email_id}", load, ready)


def single_sent_reply(detail: dict[str, Any], reply_type: str) -> dict[str, Any]:
    replies = [
        reply
        for reply in detail.get("reply_records", [])
        if reply.get("reply_type") == reply_type and reply.get("send_status") == "sent"
    ]
    if len(replies) != 1:
        raise E2EFailure(f"Expected one sent {reply_type} reply, got {len(replies)}")
    return replies[0]


def exact_recipient(reply: dict[str, Any]) -> bool:
    to_addresses = [address.lower() for _, address in getaddresses([reply.get("to_addresses") or ""]) if address]
    cc_addresses = [address.lower() for _, address in getaddresses([reply.get("cc_addresses") or ""]) if address]
    return to_addresses == [TEST_RECIPIENT] and not cc_addresses


def exact_test_transport_subject(detail: dict[str, Any], reply: dict[str, Any]) -> bool:
    outgoing_email_id = int(reply.get("outgoing_email_id") or 0)
    if not outgoing_email_id:
        return False
    outgoing = next(
        (
            row
            for row in detail.get("email_timeline", [])
            if int(row.get("id") or 0) == outgoing_email_id
        ),
        None,
    )
    return bool(
        outgoing
        and str(outgoing.get("subject") or "").strip().upper().startswith("[TEST ONLY]")
    )


def validate_complete_path(value: dict[str, Any]) -> dict[str, Any]:
    email_detail = value["email_detail"]
    detail = value["ticket_detail"]
    email = email_detail.get("email") or {}
    ticket = detail.get("ticket") or {}
    if email.get("intent_type") != "new_repair":
        raise E2EFailure("Complete fixture was not classified as new_repair")
    if ticket.get("current_status_code") != "rma_sent":
        raise E2EFailure("Complete ticket is not rma_sent after RMA reply delivery")
    sent_rmas = [
        row
        for row in detail.get("rma_records") or []
        if row.get("status") == "issued"
    ]
    if len(sent_rmas) != 1:
        raise E2EFailure("Complete ticket does not have exactly one issued RMA")
    if ticket.get("missing_fields") or not ticket.get("customer_code"):
        raise E2EFailure("Complete ticket still has missing fields or lacks SN-derived customer code")
    if not ticket.get("safety_check_hash") or not ticket.get("sn_validation_hash"):
        raise E2EFailure("Complete ticket lacks safety/SN validation hashes")
    if not detail.get("items") or any(not item.get("sn") or not item.get("material_code") for item in detail["items"]):
        raise E2EFailure("Complete ticket items were not enriched from SN master data")
    reply = single_sent_reply(detail, "rma_authorization")
    if not exact_recipient(reply) or not exact_test_transport_subject(detail, reply):
        raise E2EFailure("RMA reply did not preserve the exact test envelope and subject prefix")
    parent_message_id = str(email.get("message_id") or "")
    if (
        reply.get("in_reply_to") != parent_message_id
        or parent_message_id not in str(reply.get("references_header") or "")
    ):
        raise E2EFailure("RMA reply did not preserve the original message thread")
    if not reply.get("rma_pdf_oss_object_id") or not reply.get("rma_pdf_data_snapshot"):
        raise E2EFailure("RMA reply does not contain an archived PDF and snapshot")
    if (
        reply.get("archive_status") != "archived"
        or not reply.get("archive_verified_at")
        or not reply.get("outgoing_email_id")
        or not reply.get("smtp_message_id")
    ):
        raise E2EFailure("RMA reply archive evidence is incomplete")
    rma = sent_rmas[0]
    if (
        rma.get("pdf_validation_status") != "passed"
        or rma.get("pdf_archive_status") != "archived"
        or not rma.get("pdf_sha256")
        or not rma.get("pdf_archived_at")
        or not rma.get("issued_at")
    ):
        raise E2EFailure("Issued RMA PDF evidence is incomplete")
    issue_summary = detail.get("rma_issue_summary") or {}
    required_issue_facts = {
        "rma_received",
        "pdf_validated",
        "smtp_sent",
        "message_id_saved",
        "pdf_archived",
        "outbound_archived",
    }
    missing_issue_facts = sorted(
        fact for fact in required_issue_facts if issue_summary.get(fact) is not True
    )
    if missing_issue_facts:
        raise E2EFailure(
            "RMA issue closure evidence is incomplete: " + ",".join(missing_issue_facts)
        )
    status_logs = detail.get("status_logs") or []
    if not any(
        row.get("to_status_code") == "rma_sent"
        and row.get("trigger_event") == "rma_reply_sent"
        for row in status_logs
    ):
        raise E2EFailure("Complete ticket lacks the rma_sent transition evidence")
    if any(
        row.get("to_status_code") == "closed"
        and row.get("trigger_event") == "rma_issued_and_archived"
        for row in status_logs
    ):
        raise E2EFailure("Complete ticket was prematurely closed after RMA archival")
    return {"ticket": ticket, "thread": detail.get("thread") or {}, "reply": reply}


def validate_missing_path(value: dict[str, Any]) -> dict[str, Any]:
    email_detail = value["email_detail"]
    detail = value["ticket_detail"]
    email = email_detail.get("email") or {}
    ticket = detail.get("ticket") or {}
    if email.get("intent_type") != "new_repair":
        raise E2EFailure("Missing-field fixture was not classified as new_repair")
    if ticket.get("current_status_code") != "auto_replied" or int(ticket.get("followup_count") or 0) != 1:
        raise E2EFailure("Missing-field ticket did not send exactly one follow-up")
    missing_keys = set((ticket.get("missing_fields") or {}).keys())
    if not missing_keys or not missing_keys.issubset(CUSTOMER_REQUIRED_FIELD_SET):
        raise E2EFailure(
            f"Missing-field fixture must contain only customer-actionable required fields, got {ticket.get('missing_fields')}"
        )
    reply = single_sent_reply(detail, "missing_fields")
    if (
        not exact_recipient(reply)
        or reply.get("rma_pdf_oss_object_id")
        or not exact_test_transport_subject(detail, reply)
    ):
        raise E2EFailure("Missing-field reply envelope or attachment state is invalid")
    parent_message_id = str(email.get("message_id") or "")
    if (
        reply.get("in_reply_to") != parent_message_id
        or parent_message_id not in str(reply.get("references_header") or "")
    ):
        raise E2EFailure("Missing-field reply did not preserve the original message thread")
    if set((reply.get("missing_fields") or {}).keys()) != missing_keys:
        raise E2EFailure("Follow-up reply did not ask exactly for the remaining required fields")
    return {"ticket": ticket, "thread": detail.get("thread") or {}, "reply": reply}


def current_config(client: Client) -> dict[str, Any]:
    value = client.data("GET", "/api/v1/system/config")
    if not isinstance(value, dict):
        raise E2EFailure("System config response is invalid")
    return value


def patch_config(client: Client, **values: Any) -> dict[str, Any]:
    value = client.data("PATCH", "/api/v1/system/config", body=values)
    if not isinstance(value, dict):
        raise E2EFailure("System config update response is invalid")
    return value


def retry_preflight_action(label: str, action: Callable[[], Any]) -> Any:
    last_error: E2EFailure | None = None
    for attempt in range(1, 4):
        try:
            return action()
        except E2EFailure as exc:
            last_error = exc
            if "HTTP 409" not in str(exc) or attempt == 3:
                raise
            time.sleep(5)
    raise E2EFailure(f"{label} failed after bounded retries") from last_error


def run() -> int:
    complete_message_id, missing_message_id = require_environment()
    client = Client()
    login(client)
    initial = current_config(client)
    success = False
    evidence: dict[str, Any] = {}
    try:
        patch_config(client, auto_send_enabled=False, auto_followup_enabled=False)
        if find_email(client, complete_message_id) or find_email(client, missing_message_id):
            raise E2EFailure("One or both fixture Message-IDs already exist in the database; use fresh fixtures")
        preflight = retry_preflight_action(
            "mail preflight",
            lambda: client.data("POST", "/api/v1/system/mail-test/preflight"),
        )
        assert_database_preflight(preflight)

        retry_preflight_action(
            "ordinary reply enablement",
            lambda: patch_config(
                client,
                auto_send_enabled=True,
                auto_followup_enabled=True,
                rma_auto_send_enabled=True,
            ),
        )
        complete_email_id = fetch_exact_message(client, complete_message_id)
        complete_value = wait_for_ticket(
            client,
            complete_email_id,
        expected_status="rma_sent",
            expected_reply_type="rma_authorization",
        )
        complete = validate_complete_path(complete_value)

        retry_preflight_action(
            "automatic follow-up enablement",
            lambda: patch_config(client, auto_followup_enabled=True),
        )
        missing_email_id = fetch_exact_message(client, missing_message_id)
        missing_value = wait_for_ticket(
            client,
            missing_email_id,
            expected_status="auto_replied",
            expected_reply_type="missing_fields",
        )
        missing = validate_missing_path(missing_value)

        if complete["thread"].get("id") == missing["thread"].get("id"):
            raise E2EFailure("The same-subject fixtures were incorrectly placed in one thread")
        if complete["ticket"].get("id") == missing["ticket"].get("id"):
            raise E2EFailure("The same-subject fixtures were incorrectly placed in one ticket")

        before_reply_ids = {
            int(complete["reply"]["id"]),
            int(missing["reply"]["id"]),
        }
        if len(before_reply_ids) != 2:
            raise E2EFailure("The controlled run did not produce exactly two sent replies")
        fetch_exact_message(client, complete_message_id, expect_duplicate=True)
        fetch_exact_message(client, missing_message_id, expect_duplicate=True)
        complete_after = client.data("GET", f"/api/v1/tickets/{int(complete['ticket']['id'])}")
        missing_after = client.data("GET", f"/api/v1/tickets/{int(missing['ticket']['id'])}")
        after_sent = {
            int(reply["id"])
            for detail in (complete_after, missing_after)
            for reply in detail.get("reply_records", [])
            if reply.get("send_status") == "sent"
        }
        if after_sent != before_reply_ids or int((missing_after.get("ticket") or {}).get("followup_count") or 0) != 1:
            raise E2EFailure("Duplicate fetch changed sent replies or follow-up count")

        evidence = {
            "complete": {
                "message_id": complete_message_id,
                "email_id": complete_email_id,
                "ticket_id": complete["ticket"].get("id"),
                "reply_id": complete["reply"].get("id"),
                "smtp_message_id": complete["reply"].get("smtp_message_id"),
                "rma_pdf_snapshot": complete["reply"].get("rma_pdf_data_snapshot"),
            },
            "missing": {
                "message_id": missing_message_id,
                "email_id": missing_email_id,
                "ticket_id": missing["ticket"].get("id"),
                "reply_id": missing["reply"].get("id"),
                "smtp_message_id": missing["reply"].get("smtp_message_id"),
                "followup_count": missing["ticket"].get("followup_count"),
            },
            "sent_reply_count": len(before_reply_ids),
            "manual_mailbox_verification_required": True,
        }
        success = True
        return 0
    finally:
        safe_values = {
            "auto_send_enabled": bool(initial.get("auto_send_enabled")),
            "auto_followup_enabled": bool(initial.get("auto_followup_enabled")),
            "rma_auto_send_enabled": bool(initial.get("rma_auto_send_enabled")),
        }
        try:
            final_config = patch_config(client, **safe_values)
        except Exception as exc:
            final_config = {"restore_failed": exc.__class__.__name__, **safe_values}
        report = {
            "status": "passed" if success else "failed",
            "initial_config": {
                "auto_send_enabled": initial.get("auto_send_enabled"),
                "auto_followup_enabled": initial.get("auto_followup_enabled"),
                "rma_auto_send_enabled": initial.get("rma_auto_send_enabled"),
            },
            "final_config": {
                "auto_send_enabled": final_config.get("auto_send_enabled"),
                "auto_followup_enabled": final_config.get("auto_followup_enabled"),
                "rma_auto_send_enabled": final_config.get("rma_auto_send_enabled"),
                "restore_failed": final_config.get("restore_failed"),
            },
            "evidence": evidence,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except E2EFailure as exc:
        print(json.dumps({"status": "failed", "error_code": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
