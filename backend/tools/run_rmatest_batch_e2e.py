from __future__ import annotations

import argparse
import asyncio
import hashlib
import imaplib
import json
import os
import re
import ssl
from datetime import date, datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from sqlalchemy import delete, func, select, update

from app.config import settings
from app.core.database import AsyncSessionLocal
from app.models import (
    Base,
    BoardCard,
    CustomerServicePolicy,
    JobRunLog,
    RepairTicketItem,
    SnAsset,
    SnValidationResult,
)
from app.services.mail_safety import (
    TEST_MAIL_RECIPIENT,
    TEST_MAIL_SENDER,
    test_mail_configuration_reasons,
    test_only_subject,
)
from app.services.runtime_config import read_runtime_config
from tools.run_new_repair_mail_e2e import (
    Client,
    assert_database_preflight,
    current_config,
    exact_recipient,
    fetch_exact_message,
    find_email,
    login,
    patch_config,
    validate_complete_path,
    validate_missing_path,
    wait_for_ticket,
)


INTENTS = {
    "new_repair",
    "customer_supplement",
    "normal_reply",
    "rma_sent",
    "device_received",
    "irrelevant",
    "unknown",
}
SUBTYPES = {None, "general_irrelevant", "out_of_scope_repair"}
SEND_MODES = {
    "none",
    "manual",
    "auto_canary",
    "auto_rma",
    "auto_followup_recovery",
}
MESSAGE_ID_PATTERN = re.compile(r"^<[^<>\s]+>$")


class BatchError(RuntimeError):
    pass


def _optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _evidence_root(batch_id: str) -> Path:
    root = Path(settings.BACKEND_DIR if hasattr(settings, "BACKEND_DIR") else Path(__file__).parents[1])
    return root.parent / "test-results" / batch_id


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _mail_gate() -> None:
    reasons = test_mail_configuration_reasons()
    if reasons:
        raise BatchError("MAIL_SAFETY_GATE_FAILED:" + ",".join(reasons))
    runtime = read_runtime_config()
    if runtime["auto_send_enabled"] or runtime["auto_followup_enabled"]:
        raise BatchError("INVENTORY_REQUIRES_ALL_AUTO_SEND_DISABLED")


async def capture_database_snapshot() -> dict[str, Any]:
    tables: dict[str, dict[str, int | None]] = {}
    async with AsyncSessionLocal() as session:
        for table in Base.metadata.sorted_tables:
            count = int(await session.scalar(select(func.count()).select_from(table)) or 0)
            max_id = (
                await session.scalar(select(func.max(table.c.id)))
                if "id" in table.c
                else None
            )
            tables[table.name] = {
                "count": count,
                "max_id": int(max_id) if max_id is not None else None,
            }
        job_status_rows = (
            await session.execute(
                select(JobRunLog.status, func.count())
                .group_by(JobRunLog.status)
                .order_by(JobRunLog.status)
            )
        ).all()
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
        "job_status_counts": {str(status): int(count) for status, count in job_status_rows},
    }


def inventory(
    batch_id: str,
    *,
    limit: int,
    since_uid: int | None = None,
    include_seen: bool = False,
    exclude_manifest: Path | None = None,
) -> Path:
    _mail_gate()
    client = imaplib.IMAP4_SSL(
        settings.IMAP_HOST,
        settings.IMAP_PORT,
        ssl_context=ssl.create_default_context(),
        timeout=30,
    )
    try:
        client.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
        status, select_data = client.select(settings.IMAP_FOLDER, readonly=True)
        if status != "OK":
            raise BatchError("IMAP_READ_ONLY_SELECT_FAILED")
        uid_validity_response = client.response("UIDVALIDITY")
        uid_validity = (
            uid_validity_response[1][0].decode("ascii", errors="replace")
            if uid_validity_response[1]
            else None
        )
        status, data = client.uid("search", None, "ALL" if include_seen else "UNSEEN")
        if status != "OK":
            raise BatchError("IMAP_UNSEEN_SEARCH_FAILED")
        uids = (data[0] or b"").split()
        if since_uid is not None:
            uids = [uid for uid in uids if int(uid) > since_uid]
        excluded_message_ids: set[str] = set()
        if exclude_manifest is not None:
            previous = json.loads(exclude_manifest.read_text(encoding="utf-8"))
            excluded_message_ids = {
                str(item.get("message_id") or "")
                for item in previous.get("messages") or []
            }
        candidates = uids[-max(1, limit) :]
        messages: list[dict[str, Any]] = []
        for uid in candidates:
            status, fetched = client.uid(
                "fetch",
                uid,
                "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM TO CC DATE IN-REPLY-TO REFERENCES CONTENT-TYPE)] RFC822.SIZE BODYSTRUCTURE)",
            )
            if status != "OK" or not fetched:
                raise BatchError(f"IMAP_HEADER_FETCH_FAILED:{uid.decode()}")
            header_bytes = next(
                (part[1] for part in fetched if isinstance(part, tuple) and isinstance(part[1], bytes)),
                b"",
            )
            parsed = BytesParser(policy=policy.default).parsebytes(header_bytes)
            message_id = str(parsed.get("Message-ID") or "").strip()
            if message_id in excluded_message_ids:
                continue
            metadata = b" ".join(
                part[0] for part in fetched if isinstance(part, tuple) and isinstance(part[0], bytes)
            )
            size_match = re.search(rb"RFC822\.SIZE\s+(\d+)", metadata)
            messages.append(
                {
                    "uid": uid.decode("ascii"),
                    "message_id": message_id,
                    "header_sha256": hashlib.sha256(header_bytes).hexdigest(),
                    "received_header_date": str(parsed.get("Date") or ""),
                    "subject": str(parsed.get("Subject") or ""),
                    "from": str(parsed.get("From") or ""),
                    "to": str(parsed.get("To") or ""),
                    "cc": str(parsed.get("Cc") or ""),
                    "in_reply_to": str(parsed.get("In-Reply-To") or ""),
                    "references": str(parsed.get("References") or ""),
                    "content_type": str(parsed.get_content_type()),
                    "size_bytes": int(size_match.group(1)) if size_match else None,
                    "gold": {
                        "expected_intent": None,
                        "expected_subtype": None,
                        "expected_fields": {},
                        "expected_items": [],
                        "missing_fields": [],
                        "create_ticket": None,
                        "reply_allowed": None,
                        "send_mode": "none",
                        "expected_outbound_count": 0,
                        "temporary_sn_assets": [],
                        "temporary_board_cards": [],
                        "expected_final_status": None,
                        "expected_manual_action": None,
                    },
                }
            )
        frozen = [str(item["uid"]).encode("ascii") for item in messages]
        manifest = {
            "schema_version": 1,
            "batch_id": batch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mailbox": TEST_MAIL_SENDER,
            "folder": settings.IMAP_FOLDER,
            "uid_validity": uid_validity,
            "frozen_uid_max": frozen[-1].decode("ascii") if frozen else None,
            "since_uid_exclusive": since_uid,
            "included_seen_messages": include_seen,
            "excluded_manifest": str(exclude_manifest) if exclude_manifest else None,
            "recipient_only": TEST_MAIL_RECIPIENT,
            "business_gold_approved": False,
            "approved_by": None,
            "approved_at": None,
            "max_actual_sends": 0,
            "messages": messages,
        }
        target = _evidence_root(batch_id) / "manifest.json"
        _write_json(target, manifest)
        _write_json(
            target.parent / "inventory-summary.json",
            {
                "batch_id": batch_id,
                "uid_validity": uid_validity,
                "frozen_uid_max": manifest["frozen_uid_max"],
                "since_uid_exclusive": since_uid,
                "included_seen_messages": include_seen,
                "excluded_message_count": len(excluded_message_ids),
                "message_count": len(messages),
                "messages_sent": 0,
                "mailbox_flags_modified": False,
            },
        )
        return target
    finally:
        try:
            client.logout()
        except Exception:
            pass


def validate_manifest(path: Path, *, require_approval: bool = False) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if manifest.get("mailbox", "").lower() != TEST_MAIL_SENDER:
        errors.append("MAILBOX_MUST_BE_RMATEST1")
    if manifest.get("recipient_only", "").lower() != TEST_MAIL_RECIPIENT:
        errors.append("RECIPIENT_MUST_BE_RMATEST2")
    messages = manifest.get("messages")
    if not isinstance(messages, list) or not messages:
        errors.append("MESSAGES_REQUIRED")
        messages = []
    ids: set[str] = set()
    auto_canaries = 0
    planned_sends = 0
    for index, item in enumerate(messages):
        prefix = f"messages[{index}]"
        message_id = str(item.get("message_id") or "")
        if not MESSAGE_ID_PATTERN.fullmatch(message_id):
            errors.append(f"{prefix}.message_id_INVALID")
        if message_id in ids:
            errors.append(f"{prefix}.message_id_DUPLICATE")
        ids.add(message_id)
        gold = item.get("gold") if isinstance(item.get("gold"), dict) else {}
        intent = gold.get("expected_intent")
        subtype = gold.get("expected_subtype")
        if intent not in INTENTS:
            errors.append(f"{prefix}.expected_intent_REQUIRED")
        if subtype not in SUBTYPES:
            errors.append(f"{prefix}.expected_subtype_INVALID")
        if intent == "irrelevant" and subtype not in {"general_irrelevant", "out_of_scope_repair"}:
            errors.append(f"{prefix}.irrelevant_subtype_REQUIRED")
        if intent != "irrelevant" and subtype is not None:
            errors.append(f"{prefix}.non_irrelevant_subtype_MUST_BE_NULL")
        for required in ("create_ticket", "reply_allowed", "expected_final_status", "expected_manual_action"):
            if gold.get(required) is None:
                errors.append(f"{prefix}.{required}_REQUIRED")
        mode = gold.get("send_mode")
        if mode not in SEND_MODES:
            errors.append(f"{prefix}.send_mode_INVALID")
        expected_outbound_count = gold.get("expected_outbound_count")
        if not isinstance(expected_outbound_count, int) or expected_outbound_count < 0:
            errors.append(f"{prefix}.expected_outbound_count_INVALID")
            expected_outbound_count = 0
        planned_sends += expected_outbound_count
        if mode == "auto_canary":
            auto_canaries += 1
            if intent != "new_repair" or gold.get("expected_final_status") != "rma_sent":
                errors.append(f"{prefix}.auto_canary_MUST_BE_COMPLETE_NEW_REPAIR")
    maximum = manifest.get("max_actual_sends")
    if not isinstance(maximum, int) or maximum != planned_sends:
        errors.append("MAX_ACTUAL_SENDS_MUST_EQUAL_PLANNED_SENDS")
    if auto_canaries > 1:
        errors.append("ONLY_ONE_AUTO_CANARY_ALLOWED")
    if require_approval and (
        manifest.get("business_gold_approved") is not True
        or not manifest.get("approved_by")
        or not manifest.get("approved_at")
    ):
        errors.append("BUSINESS_GOLD_APPROVAL_REQUIRED")
    result = {
        "valid": not errors,
        "errors": errors,
        "message_count": len(messages),
        "planned_sends": planned_sends,
        "auto_canaries": auto_canaries,
    }
    if errors:
        raise BatchError("MANIFEST_INVALID:" + ",".join(errors))
    return result


def execute_gate(path: Path) -> dict[str, Any]:
    _mail_gate()
    result = validate_manifest(path, require_approval=True)
    if os.getenv("RUN_REAL_MAIL_INTEGRATION_TESTS") != "1":
        raise BatchError("RUN_REAL_MAIL_INTEGRATION_TESTS_MUST_EQUAL_1")
    if os.getenv("E2E_GOLD_MANIFEST_APPROVED") != "1":
        raise BatchError("E2E_GOLD_MANIFEST_APPROVED_MUST_EQUAL_1")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return {
        **result,
        "status": "approved_for_staged_execution",
        "message_ids": [item["message_id"] for item in manifest["messages"]],
        "note": "Use exact Message-ID fetch jobs only; sending remains subject to per-message approval.",
    }


def _temporary_master_rows(
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    sn_rows: dict[str, dict[str, Any]] = {}
    board_rows: dict[str, dict[str, Any]] = {}
    policy_rows: dict[str, dict[str, Any]] = {}
    for message in manifest["messages"]:
        gold = message["gold"]
        for row in gold.get("temporary_sn_assets") or []:
            sn = str(row.get("sn") or "").strip().upper()
            if not sn:
                raise BatchError("TEMPORARY_SN_REQUIRES_SN")
            if sn in sn_rows and sn_rows[sn] != row:
                raise BatchError(f"TEMPORARY_SN_CONFLICT:{sn}")
            sn_rows[sn] = row
        for row in gold.get("temporary_board_cards") or []:
            material = str(row.get("material_code") or "").strip()
            if not material:
                raise BatchError("TEMPORARY_BOARD_CARD_REQUIRES_MATERIAL_CODE")
            if material in board_rows and board_rows[material] != row:
                raise BatchError(f"TEMPORARY_BOARD_CARD_CONFLICT:{material}")
            board_rows[material] = row
        for row in gold.get("temporary_customer_policies") or []:
            policy_code = str(row.get("policy_code") or "").strip()
            if not policy_code:
                raise BatchError("TEMPORARY_CUSTOMER_POLICY_REQUIRES_POLICY_CODE")
            if policy_code in policy_rows and policy_rows[policy_code] != row:
                raise BatchError(f"TEMPORARY_CUSTOMER_POLICY_CONFLICT:{policy_code}")
            policy_rows[policy_code] = row
    return list(sn_rows.values()), list(board_rows.values()), list(policy_rows.values())


async def apply_temporary_master_data(manifest: dict[str, Any], state_path: Path) -> dict[str, Any]:
    sn_rows, board_rows, policy_rows = _temporary_master_rows(manifest)
    batch_id = str(manifest["batch_id"])
    source_hash = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {"batch_id": batch_id, "messages": {}, "actual_send_count": 0}
    )
    created = state.setdefault(
        "temporary_master_data",
        {"sn_asset_ids": [], "board_card_ids": [], "customer_policy_ids": []},
    )
    created.setdefault("customer_policy_ids", [])
    async with AsyncSessionLocal() as session:
        for row_no, row in enumerate(sn_rows, 1):
            sn = str(row["sn"]).strip().upper()
            existing = await session.scalar(select(SnAsset).where(SnAsset.sn == sn))
            if existing is not None:
                if existing.source_file_name == batch_id and existing.id in created["sn_asset_ids"]:
                    existing.warranty_start_date = _optional_date(
                        row.get("warranty_start_date")
                    )
                    existing.warranty_end_date = _optional_date(
                        row.get("warranty_end_date")
                    )
                    existing.raw_data = {
                        "batch_id": batch_id,
                        "gold_confirmed": True,
                        "updated_for_recovery": True,
                    }
                    continue
                raise BatchError(f"TEMPORARY_SN_ALREADY_EXISTS:{sn}")
            required = ("customer_code", "customer_name", "material_code")
            missing = [field for field in required if not str(row.get(field) or "").strip()]
            if missing:
                raise BatchError(f"TEMPORARY_SN_FIELDS_REQUIRED:{sn}:{','.join(missing)}")
            asset = SnAsset(
                sn=sn,
                customer_code=str(row["customer_code"]).strip(),
                customer_name=str(row["customer_name"]).strip(),
                material_code=str(row["material_code"]).strip(),
                material_name=str(row.get("material_name") or "").strip() or None,
                asset_status="valid",
                warranty_start_date=_optional_date(row.get("warranty_start_date")),
                warranty_end_date=_optional_date(row.get("warranty_end_date")),
                source_file_name=batch_id,
                source_file_hash=source_hash,
                source_row_no=row_no,
                source_system="e2e_test",
                raw_data={"batch_id": batch_id, "gold_confirmed": True},
            )
            session.add(asset)
            await session.flush()
            created["sn_asset_ids"].append(asset.id)
        for row_no, row in enumerate(board_rows, 1):
            material = str(row["material_code"]).strip()
            existing = await session.scalar(
                select(BoardCard).where(BoardCard.material_code == material)
            )
            if existing is not None:
                if existing.source_file_name == batch_id and existing.id in created["board_card_ids"]:
                    continue
                raise BatchError(f"TEMPORARY_BOARD_CARD_ALREADY_EXISTS:{material}")
            card = BoardCard(
                material_code=material,
                material_name=str(row.get("material_name") or "").strip() or None,
                need_ship_to_beijing=bool(row.get("need_ship_to_beijing", True)),
                shipping_address=str(row.get("shipping_address") or "").strip() or None,
                shipping_contact=str(row.get("shipping_contact") or "").strip() or None,
                shipping_phone=str(row.get("shipping_phone") or "").strip() or None,
                postal_code=str(row.get("postal_code") or "").strip() or None,
                status="active",
                source_file_name=batch_id,
                source_file_hash=source_hash,
                source_row_no=row_no,
                raw_data={"batch_id": batch_id, "gold_confirmed": True},
            )
            session.add(card)
            await session.flush()
            created["board_card_ids"].append(card.id)
        for row_no, row in enumerate(policy_rows, 1):
            policy_code = str(row["policy_code"]).strip()
            existing = await session.scalar(
                select(CustomerServicePolicy).where(
                    CustomerServicePolicy.policy_code == policy_code
                )
            )
            if existing is not None:
                if (
                    existing.source_file_name == batch_id
                    and existing.id in created["customer_policy_ids"]
                ):
                    continue
                raise BatchError(
                    f"TEMPORARY_CUSTOMER_POLICY_ALREADY_EXISTS:{policy_code}"
                )
            required = (
                "customer_code",
                "customer_name",
                "policy_type",
                "repair_price",
                "currency",
                "tax_rate",
            )
            missing = [field for field in required if row.get(field) in (None, "")]
            if missing:
                raise BatchError(
                    "TEMPORARY_CUSTOMER_POLICY_FIELDS_REQUIRED:"
                    + policy_code
                    + ":"
                    + ",".join(missing)
                )
            customer_policy = CustomerServicePolicy(
                policy_code=policy_code,
                customer_code=str(row["customer_code"]).strip(),
                customer_name=str(row["customer_name"]).strip(),
                policy_type=str(row["policy_type"]).strip(),
                effective_from=row.get("effective_from"),
                effective_until=row.get("effective_until"),
                repair_price=row["repair_price"],
                currency=str(row["currency"]).strip().upper(),
                tax_rate=row["tax_rate"],
                shipping_fee_text=str(
                    row.get("shipping_fee_text") or "one-way charge/单次收费"
                ),
                reply_salutation=str(row.get("reply_salutation") or "").strip() or None,
                hide_company_name=bool(row.get("hide_company_name", False)),
                force_manual_review=bool(row.get("force_manual_review", False)),
                enabled=bool(row.get("enabled", True)),
                source_file_name=batch_id,
                source_row_no=row_no,
            )
            session.add(customer_policy)
            await session.flush()
            created["customer_policy_ids"].append(customer_policy.id)
        await session.commit()
    _write_json(state_path, state)
    return created


async def cleanup_temporary_master_data(manifest_path: Path) -> dict[str, Any]:
    validate_manifest(manifest_path, require_approval=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state_path = manifest_path.parent / "execution-state.json"
    if not state_path.exists():
        return {
            "sn_assets_deleted": 0,
            "board_cards_deleted": 0,
            "customer_policies_deleted": 0,
        }
    state = json.loads(state_path.read_text(encoding="utf-8"))
    created = state.get("temporary_master_data") or {}
    sn_ids = [int(value) for value in created.get("sn_asset_ids") or []]
    board_ids = [int(value) for value in created.get("board_card_ids") or []]
    policy_ids = [int(value) for value in created.get("customer_policy_ids") or []]
    batch_id = str(manifest["batch_id"])
    async with AsyncSessionLocal() as session:
        if sn_ids:
            assets = (
                await session.execute(select(SnAsset).where(SnAsset.id.in_(sn_ids)))
            ).scalars().all()
            if any(asset.source_file_name != batch_id for asset in assets):
                raise BatchError("TEMPORARY_SN_CLEANUP_SCOPE_MISMATCH")
            await session.execute(
                update(SnValidationResult)
                .where(SnValidationResult.matched_sn_asset_id.in_(sn_ids))
                .values(matched_sn_asset_id=None)
            )
            # Preserve the real-mail ticket and its audit trail while detaching
            # batch-scoped temporary master data before deleting that data.
            await session.execute(
                update(RepairTicketItem)
                .where(RepairTicketItem.sn_asset_id.in_(sn_ids))
                .values(sn_asset_id=None)
            )
            await session.execute(delete(SnAsset).where(SnAsset.id.in_(sn_ids)))
        if board_ids:
            cards = (
                await session.execute(select(BoardCard).where(BoardCard.id.in_(board_ids)))
            ).scalars().all()
            if any(card.source_file_name != batch_id for card in cards):
                raise BatchError("TEMPORARY_BOARD_CLEANUP_SCOPE_MISMATCH")
            await session.execute(delete(BoardCard).where(BoardCard.id.in_(board_ids)))
        if policy_ids:
            policies = (
                await session.execute(
                    select(CustomerServicePolicy).where(
                        CustomerServicePolicy.id.in_(policy_ids)
                    )
                )
            ).scalars().all()
            if any(policy.source_file_name != batch_id for policy in policies):
                raise BatchError("TEMPORARY_CUSTOMER_POLICY_CLEANUP_SCOPE_MISMATCH")
            await session.execute(
                delete(CustomerServicePolicy).where(
                    CustomerServicePolicy.id.in_(policy_ids)
                )
            )
        await session.commit()
    state["cleanup"] = {
        "sn_assets_deleted": len(sn_ids),
        "board_cards_deleted": len(board_ids),
        "customer_policies_deleted": len(policy_ids),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(state_path, state)
    return state["cleanup"]


def execute_phase(path: Path, *, phase: str, resume: bool) -> dict[str, Any]:
    gate = execute_gate(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    os.environ.setdefault("INTEGRATION_ADMIN_USERNAME", settings.DEFAULT_ADMIN_USERNAME)
    os.environ.setdefault("INTEGRATION_ADMIN_PASSWORD", settings.DEFAULT_ADMIN_PASSWORD)
    client = Client()
    login(client)
    initial = current_config(client)
    state_path = path.parent / "execution-state.json"
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if resume and state_path.exists()
        else {"batch_id": manifest["batch_id"], "messages": {}, "actual_send_count": 0}
    )
    try:
        patch_config(
            client,
            auto_send_enabled=False,
            auto_followup_enabled=False,
            rma_auto_send_enabled=True,
        )
        preflight = client.data("POST", "/api/v1/system/mail-test/preflight")
        assert_database_preflight(preflight)
        relay_status = client.data("GET", "/api/v1/system/integrations/sqlserver/status")
        if phase == "prepare":
            existing_messages = {
                item["message_id"]: (
                    {
                        "email_id": found.get("id"),
                        "parse_status": found.get("parse_status"),
                        "intent_type": found.get("intent_type"),
                    }
                    if (found := find_email(client, item["message_id"]))
                    else None
                )
                for item in manifest["messages"]
            }
            baseline = {
                "database": asyncio.run(capture_database_snapshot()),
                "runtime_status": client.data("GET", "/api/v1/system/runtime-status"),
                "mail_fetch_status": client.data("GET", "/api/v1/emails/fetch-status"),
                "runtime_config": initial,
                "relay_status": relay_status,
                "target_messages": existing_messages,
                "preflight": preflight,
            }
            baseline_path = path.parent / "baseline.json"
            _write_json(baseline_path, baseline)
            return {
                **gate,
                "status": "preflight_passed",
                "preflight": preflight,
                "relay_status": relay_status,
                "initial_runtime_config": initial,
                "baseline_path": str(baseline_path),
            }
        if phase == "master-data":
            created = asyncio.run(apply_temporary_master_data(manifest, state_path))
            return {"status": "phase_complete", "phase": phase, **created}
        if phase == "recover-auto-rma":
            if os.getenv("E2E_CANARY_APPROVED") != "1":
                raise BatchError("E2E_CANARY_APPROVED_MUST_EQUAL_1")
            candidates = [
                item
                for item in manifest["messages"]
                if item["gold"]["send_mode"] in {"auto_canary", "auto_rma"}
                and item["message_id"] not in state["messages"]
                and find_email(client, item["message_id"])
            ]
            if not candidates:
                raise BatchError("NO_ARCHIVED_AUTO_RMA_MESSAGE_TO_RECOVER")
            canary_candidates = [
                item
                for item in candidates
                if item["gold"]["send_mode"] == "auto_canary"
            ]
            target = canary_candidates[0] if canary_candidates else candidates[0]
            if int(state.get("actual_send_count") or 0) >= int(
                manifest["max_actual_sends"]
            ):
                raise BatchError("ACTUAL_SEND_HARD_LIMIT_REACHED")
            archived = find_email(client, target["message_id"])
            email_id = int(archived["id"])
            email_detail = client.data("GET", f"/api/v1/emails/{email_id}")
            ticket_ids = [
                row.get("ticket_id")
                for row in email_detail.get("parse_results", [])
                if row.get("ticket_id")
            ]
            if not ticket_ids:
                raise BatchError("RECOVERY_TICKET_NOT_FOUND")
            ticket_id = int(ticket_ids[0])
            patch_config(
                client,
                auto_send_enabled=True,
                auto_followup_enabled=False,
                rma_auto_send_enabled=True,
            )
            existing_detail = client.data("GET", f"/api/v1/tickets/{ticket_id}")
            existing_ticket = existing_detail.get("ticket") or {}
            already_sent = [
                row
                for row in existing_detail.get("reply_records", [])
                if row.get("reply_type") == "rma_authorization"
                and row.get("send_status") == "sent"
            ]
            if (
                existing_ticket.get("current_status_code") == "rma_sent"
                and len(already_sent) == 1
            ):
                normalized = client.data(
                    "POST", f"/api/v1/tickets/{ticket_id}/rma/retry-send"
                )
                if not normalized.get("idempotent_reuse"):
                    raise BatchError("RECOVERY_SENT_RMA_WAS_NOT_IDEMPOTENT")
                existing_detail = client.data(
                    "GET", f"/api/v1/tickets/{ticket_id}"
                )
                verified = validate_complete_path(
                    {
                        "email_detail": email_detail,
                        "ticket_detail": existing_detail,
                    }
                )
                ticket = verified["ticket"]
                reply = verified["reply"]
                state["messages"][target["message_id"]] = {
                    "email_id": email_id,
                    "ticket_id": ticket.get("id"),
                    "manual_task_id": None,
                    "manual_task_recovered": True,
                    "warranty_task_id": None,
                    "warranty_task_recovered": True,
                    "rma_rule_retry_queued": False,
                    "final_status": ticket.get("current_status_code"),
                    "reply_id": reply.get("id"),
                    "reply_message_id": reply.get("smtp_message_id"),
                    "rma_pdf_oss_object_id": reply.get(
                        "rma_pdf_oss_object_id"
                    ),
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                }
                state["actual_send_count"] = int(
                    state.get("actual_send_count") or 0
                ) + 1
                _write_json(state_path, state)
                return {
                    "status": "phase_complete",
                    "phase": phase,
                    "message_id": target["message_id"],
                    "ticket_id": ticket.get("id"),
                    "actual_send_count": state["actual_send_count"],
                    "idempotent_recovery": True,
                }
            warranty_tasks = [
                row
                for row in existing_detail.get("manual_tasks", [])
                if row.get("status")
                in {"pending", "assigned", "claimed", "assignment_failed"}
                and row.get("task_type") == "warranty_status_unknown"
            ]
            recovered_warranty_task_id: int | None = None
            if warranty_tasks:
                if len(warranty_tasks) != 1:
                    raise BatchError(
                        f"RECOVERY_REQUIRES_ONE_WARRANTY_TASK:{len(warranty_tasks)}"
                    )
                refreshed = client.data(
                    "POST", f"/api/v1/tickets/{ticket_id}/validate-sn"
                )
                if (refreshed.get("ticket") or {}).get(
                    "sn_validation_status"
                ) != "passed":
                    raise BatchError("RECOVERY_WARRANTY_SN_REFRESH_FAILED")
                recovered_warranty_task_id = int(warranty_tasks[0]["id"])
                resolved_warranty = client.data(
                    "POST",
                    (
                        "/api/v1/manual-review/tasks/"
                        f"{recovered_warranty_task_id}/resolve"
                    ),
                    body={
                        "resolution": (
                            f"{manifest['batch_id']} temporary warranty evidence "
                            "added and validated"
                        ),
                        "resolution_type": "e2e_test_warranty_evidence",
                        "next_action": "transition_ready_for_export",
                        "result_payload": {
                            "batch_id": manifest["batch_id"],
                            "temporary_master_data": True,
                        },
                    },
                )
                if (resolved_warranty.get("ticket") or {}).get(
                    "current_status_code"
                ) != "ready_for_export":
                    raise BatchError("RECOVERY_WARRANTY_TASK_NOT_RESOLVED")
                existing_detail = client.data("GET", f"/api/v1/tickets/{ticket_id}")
                existing_ticket = existing_detail.get("ticket") or {}
            rma_rule_retry_queued = False
            has_unsent_rma = bool(existing_detail.get("rma_records")) and not [
                row
                for row in existing_detail.get("reply_records", [])
                if row.get("reply_type") == "rma_authorization"
                and row.get("send_status") == "sent"
            ]
            pending_rma_replies = [
                row
                for row in existing_detail.get("reply_records", [])
                if row.get("reply_type") == "rma_authorization"
                and row.get("send_status")
                in {"pending_review", "approved_pending_send"}
            ]
            if has_unsent_rma and pending_rma_replies:
                if len(pending_rma_replies) != 1:
                    raise BatchError(
                        "RECOVERY_REQUIRES_ONE_PENDING_RMA_REPLY"
                    )
                approved = client.data(
                    "POST",
                    (
                        "/api/v1/replies/"
                        f"{int(pending_rma_replies[0]['id'])}/approve-send"
                    ),
                )
                approved_reply = approved.get("reply") or {}
                if approved_reply.get("send_status") != "sent":
                    raise BatchError("RECOVERY_PENDING_RMA_SEND_FAILED")
                existing_detail = client.data(
                    "GET", f"/api/v1/tickets/{ticket_id}"
                )
                existing_ticket = existing_detail.get("ticket") or {}
                has_unsent_rma = False
            if has_unsent_rma:
                refreshed = client.data(
                    "POST", f"/api/v1/tickets/{ticket_id}/validate-sn"
                )
                refreshed_ticket = refreshed.get("ticket") or {}
                if refreshed_ticket.get("sn_validation_status") != "passed":
                    raise BatchError("RECOVERY_RMA_RULE_SN_REFRESH_FAILED")
                expected_warranty = {
                    str(row.get("sn") or "").strip().upper(): (
                        row.get("warranty_start_date"),
                        row.get("warranty_end_date"),
                    )
                    for row in target.get("gold", {}).get(
                        "temporary_sn_assets", []
                    )
                    if row.get("warranty_start_date")
                    or row.get("warranty_end_date")
                }
                refreshed_checks = (
                    refreshed_ticket.get("sn_validation_snapshot") or {}
                ).get("checks") or []
                refreshed_by_sn = {
                    str(row.get("sn") or "").strip().upper(): row
                    for row in refreshed_checks
                }
                for sn, (expected_start, expected_end) in expected_warranty.items():
                    check = refreshed_by_sn.get(sn) or {}
                    if (
                        str(check.get("warranty_start_date") or "")
                        != str(expected_start or "")
                        or str(check.get("warranty_end_date") or "")
                        != str(expected_end or "")
                    ):
                        raise BatchError(
                            f"RECOVERY_WARRANTY_SNAPSHOT_STALE:{sn}"
                        )
                safety = client.data(
                    "POST", f"/api/v1/tickets/{ticket_id}/validate-export"
                )
                if safety.get("status") != "ready_for_export":
                    raise BatchError(
                        "RECOVERY_RMA_RULE_SAFETY_FAILED:"
                        + str(safety.get("status"))
                    )
                client.data(
                    "POST", f"/api/v1/tickets/{ticket_id}/rma/retry-send"
                )
                rma_rule_retry_queued = True
                existing_detail = client.data("GET", f"/api/v1/tickets/{ticket_id}")
                existing_ticket = existing_detail.get("ticket") or {}
            validated = (
                client.data("POST", f"/api/v1/tickets/{ticket_id}/validate-sn")
                if existing_ticket.get("current_status_code") == "manual_review"
                else existing_detail
            )
            ticket = validated.get("ticket") or {}
            if ticket.get("sn_validation_status") != "passed":
                raise BatchError("RECOVERY_SN_VALIDATION_NOT_PASSED")
            task_id: int | None = None
            if ticket.get("current_status_code") == "manual_review":
                tasks = [
                    row
                    for row in validated.get("manual_tasks", [])
                    if row.get("status")
                    in {"pending", "assigned", "claimed", "assignment_failed"}
                ]
                if len(tasks) != 1:
                    raise BatchError(
                        f"RECOVERY_REQUIRES_ONE_OPEN_MANUAL_TASK:{len(tasks)}"
                    )
                task_id = int(tasks[0]["id"])
                resolved = client.data(
                    "POST",
                    f"/api/v1/manual-review/tasks/{task_id}/resolve",
                    body={
                        "resolution": (
                            f"{manifest['batch_id']} temporary SN/customer policy "
                            "added and SN validation passed"
                        ),
                        "resolution_type": "e2e_test_master_data",
                        "next_action": "transition_ready_for_export",
                        "result_payload": {
                            "batch_id": manifest["batch_id"],
                            "temporary_master_data": True,
                        },
                    },
                )
                safety_result = resolved.get("safety_result")
                resolved_ticket = resolved.get("ticket") or {}
                if safety_result and safety_result.get("status") != "ready_for_export":
                    raise BatchError(
                        "RECOVERY_EXPORT_SAFETY_NOT_PASSED:"
                        + str(safety_result.get("status"))
                    )
                if resolved_ticket.get("current_status_code") != "ready_for_export":
                    post_resolution = client.data(
                        "GET", f"/api/v1/tickets/{ticket_id}"
                    )
                    post_ticket = post_resolution.get("ticket") or {}
                    if post_ticket.get("current_status_code") not in {
                        "ready_for_export",
                        "rma_sent",
                    }:
                        raise BatchError(
                            "RECOVERY_TICKET_NOT_READY_FOR_EXPORT"
                        )
            elif ticket.get("current_status_code") not in {
                "ready_for_export",
                "rma_sent",
            }:
                raise BatchError(
                    "RECOVERY_TICKET_STATUS_UNSUPPORTED:"
                    + str(ticket.get("current_status_code"))
                )
            current = client.data("GET", f"/api/v1/tickets/{ticket_id}")
            current_ticket = current.get("ticket") or {}
            sap_summary = current.get("sap_export_summary") or {}
            if (
                not current_ticket.get("safety_check_hash")
                or sap_summary.get("batch_status") == "superseded"
            ):
                safety = client.data(
                    "POST", f"/api/v1/tickets/{ticket_id}/validate-export"
                )
                if safety.get("status") != "ready_for_export":
                    raise BatchError(
                        "RECOVERY_EXPORT_SAFETY_REBUILD_FAILED:"
                        + str(safety.get("status"))
                    )
                current = client.data("GET", f"/api/v1/tickets/{ticket_id}")
                current_ticket = current.get("ticket") or {}
            current_summary = current.get("sap_export_summary") or {}
            if (
                current_ticket.get("relay_export_status") == "failed"
                or int(current_summary.get("failed_count") or 0) > 0
            ):
                client.data("POST", f"/api/v1/tickets/{ticket_id}/sap-export/retry")
            completed = wait_for_ticket(
                client,
                email_id,
                expected_status="rma_sent",
                expected_reply_type="rma_authorization",
            )
            verified = validate_complete_path(completed)
            ticket = verified["ticket"]
            reply = verified["reply"]
            state["messages"][target["message_id"]] = {
                "email_id": email_id,
                "ticket_id": ticket.get("id"),
                "manual_task_id": task_id,
                "manual_task_recovered": task_id is not None,
                "warranty_task_id": recovered_warranty_task_id,
                "warranty_task_recovered": recovered_warranty_task_id is not None,
                "rma_rule_retry_queued": rma_rule_retry_queued,
                "final_status": ticket.get("current_status_code"),
                "reply_id": reply.get("id"),
                "reply_message_id": reply.get("smtp_message_id"),
                "rma_pdf_oss_object_id": reply.get("rma_pdf_oss_object_id"),
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            state["actual_send_count"] = int(state.get("actual_send_count") or 0) + 1
            _write_json(state_path, state)
        elif phase == "classify":
            selected = [
                item for item in manifest["messages"]
                if item["gold"]["send_mode"] != "auto_canary"
            ]
            for item in selected:
                message_id = item["message_id"]
                if message_id in state["messages"]:
                    continue
                archived = find_email(client, message_id)
                email_id = int(archived["id"]) if archived else fetch_exact_message(client, message_id)
                detail = client.data("GET", f"/api/v1/emails/{email_id}")
                email = detail.get("email") or {}
                state["messages"][message_id] = {
                    "email_id": email_id,
                    "intent_type": email.get("intent_type"),
                    "intent_subtype": email.get("intent_subtype"),
                    "parse_status": email.get("parse_status"),
                    "terminal_reason_code": email.get("terminal_reason_code"),
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                }
                _write_json(state_path, state)
        elif phase in {"canary", "auto-rma"}:
            if os.getenv("E2E_CANARY_APPROVED") != "1":
                raise BatchError("E2E_CANARY_APPROVED_MUST_EQUAL_1")
            candidates = [
                item for item in manifest["messages"]
                if item["gold"]["send_mode"] in {"auto_canary", "auto_rma"}
                and item["message_id"] not in state["messages"]
            ]
            if not candidates:
                raise BatchError("NO_PENDING_AUTO_RMA_MESSAGE")
            canary_pending = [
                item for item in candidates
                if item["gold"]["send_mode"] == "auto_canary"
            ]
            target = canary_pending[0] if canary_pending else candidates[0]
            if target["gold"]["send_mode"] != "auto_canary":
                required_canaries = [
                    item["message_id"] for item in manifest["messages"]
                    if item["gold"]["send_mode"] == "auto_canary"
                ]
                if not set(required_canaries) <= set(state["messages"]):
                    raise BatchError("AUTO_CANARY_NOT_COMPLETE")
            if int(state.get("actual_send_count") or 0) >= int(manifest["max_actual_sends"]):
                raise BatchError("ACTUAL_SEND_HARD_LIMIT_REACHED")
            message_id = target["message_id"]
            if find_email(client, message_id):
                raise BatchError("CANARY_MESSAGE_ALREADY_ARCHIVED")
            patch_config(
                client,
                auto_send_enabled=True,
                auto_followup_enabled=False,
                rma_auto_send_enabled=True,
            )
            email_id = fetch_exact_message(client, message_id)
            completed = wait_for_ticket(
                client,
                email_id,
                expected_status="rma_sent",
                expected_reply_type="rma_authorization",
            )
            verified = validate_complete_path(completed)
            ticket = verified["ticket"]
            reply = verified["reply"]
            state["messages"][message_id] = {
                "email_id": email_id,
                "ticket_id": ticket.get("id"),
                "final_status": ticket.get("current_status_code"),
                "reply_id": reply.get("id"),
                "reply_message_id": reply.get("smtp_message_id"),
                "rma_pdf_oss_object_id": reply.get("rma_pdf_oss_object_id"),
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            state["actual_send_count"] = int(state.get("actual_send_count") or 0) + 1
            _write_json(state_path, state)
        elif phase == "auto-followup":
            candidates = [
                item for item in manifest["messages"]
                if item["gold"]["send_mode"] == "auto_followup_recovery"
                and item["message_id"] not in state["messages"]
            ]
            if len(candidates) != 1:
                raise BatchError("EXACTLY_ONE_PENDING_AUTO_FOLLOWUP_REQUIRED")
            if int(state.get("actual_send_count") or 0) >= int(manifest["max_actual_sends"]):
                raise BatchError("ACTUAL_SEND_HARD_LIMIT_REACHED")
            target = candidates[0]
            patch_config(
                client,
                auto_send_enabled=False,
                auto_followup_enabled=True,
                rma_auto_send_enabled=True,
            )
            archived = find_email(client, target["message_id"])
            email_id = (
                int(archived["id"])
                if archived
                else fetch_exact_message(client, target["message_id"])
            )
            if archived:
                email_detail = client.data("GET", f"/api/v1/emails/{email_id}")
                ticket_ids = [
                    row.get("ticket_id")
                    for row in email_detail.get("parse_results", [])
                    if row.get("ticket_id")
                ]
                if len(ticket_ids) != 1:
                    raise BatchError(
                        "FOLLOWUP_RECOVERY_REQUIRES_ONE_TICKET"
                    )
                ticket_id = int(ticket_ids[0])
                ticket_detail = client.data(
                    "GET", f"/api/v1/tickets/{ticket_id}"
                )
                ticket = ticket_detail.get("ticket") or {}
                if ticket.get("current_status_code") == "manual_review":
                    validated = client.data(
                        "POST", f"/api/v1/tickets/{ticket_id}/validate-sn"
                    )
                    validated_ticket = validated.get("ticket") or {}
                    if validated_ticket.get("sn_validation_status") != "passed":
                        raise BatchError(
                            "FOLLOWUP_RECOVERY_SN_VALIDATION_FAILED"
                        )
                    expected_missing = sorted(
                        target["gold"].get("missing_fields") or []
                    )
                    actual_missing = sorted(
                        (validated_ticket.get("missing_fields") or {}).keys()
                    )
                    if actual_missing != expected_missing:
                        raise BatchError(
                            "FOLLOWUP_RECOVERY_MISSING_FIELDS_MISMATCH"
                        )
                    open_tasks = [
                        row
                        for row in validated.get("manual_tasks", [])
                        if row.get("status")
                        in {
                            "pending",
                            "assigned",
                            "claimed",
                            "assignment_failed",
                        }
                    ]
                    if len(open_tasks) != 1:
                        raise BatchError(
                            "FOLLOWUP_RECOVERY_REQUIRES_ONE_OPEN_TASK"
                        )
                    client.data(
                        "POST",
                        (
                            "/api/v1/manual-review/tasks/"
                            f"{int(open_tasks[0]['id'])}/resolve"
                        ),
                        body={
                            "resolution": (
                                f"{manifest['batch_id']} temporary SN data "
                                "added; only mailing_address remains missing"
                            ),
                            "resolution_type": "e2e_test_master_data",
                            "next_action": "generate_followup",
                            "result_payload": {
                                "batch_id": manifest["batch_id"],
                                "temporary_master_data": True,
                            },
                        },
                    )
            completed = wait_for_ticket(
                client,
                email_id,
                expected_status="auto_replied",
                expected_reply_type="missing_fields",
            )
            verified = validate_missing_path(completed)
            ticket = verified["ticket"]
            reply = verified["reply"]
            missing = sorted((ticket.get("missing_fields") or {}).keys())
            expected_missing = sorted(target["gold"].get("missing_fields") or [])
            if missing != expected_missing:
                raise BatchError(
                    "FOLLOWUP_MISSING_FIELDS_MISMATCH:"
                    + ",".join(missing)
                    + ":expected="
                    + ",".join(expected_missing)
                )
            state["messages"][target["message_id"]] = {
                "email_id": email_id,
                "ticket_id": ticket.get("id"),
                "final_status": ticket.get("current_status_code"),
                "missing_fields": missing,
                "reply_id": reply.get("id"),
                "reply_message_id": reply.get("smtp_message_id"),
                "awaiting_customer_supplement": True,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            state["actual_send_count"] = int(state.get("actual_send_count") or 0) + 1
            _write_json(state_path, state)
        elif phase == "supplement-rma":
            supplement_message_id = os.getenv(
                "E2E_SUPPLEMENT_MESSAGE_ID", ""
            ).strip()
            if not MESSAGE_ID_PATTERN.fullmatch(supplement_message_id):
                raise BatchError("E2E_SUPPLEMENT_MESSAGE_ID_REQUIRED")
            awaiting = [
                (message_id, row)
                for message_id, row in state["messages"].items()
                if row.get("awaiting_customer_supplement")
            ]
            if len(awaiting) != 1:
                raise BatchError(
                    "EXACTLY_ONE_AWAITING_SUPPLEMENT_REQUIRED"
                )
            original_message_id, original_state = awaiting[0]
            if int(state.get("actual_send_count") or 0) + 1 != int(
                manifest["max_actual_sends"]
            ):
                raise BatchError("SUPPLEMENT_RMA_MUST_BE_FINAL_SEND")
            patch_config(
                client,
                auto_send_enabled=True,
                auto_followup_enabled=False,
                rma_auto_send_enabled=True,
            )
            archived = find_email(client, supplement_message_id)
            supplement_email_id = (
                int(archived["id"])
                if archived
                else fetch_exact_message(client, supplement_message_id)
            )
            completed = wait_for_ticket(
                client,
                supplement_email_id,
                expected_status="rma_sent",
                expected_reply_type="rma_authorization",
            )
            supplement_email = (
                completed["email_detail"].get("email") or {}
            )
            if supplement_email.get("intent_type") != "customer_supplement":
                raise BatchError("SUPPLEMENT_INTENT_MISMATCH")
            detail = completed.get("ticket_detail") or {}
            ticket = detail.get("ticket") or {}
            if int(ticket.get("id") or 0) != int(
                original_state["ticket_id"]
            ):
                raise BatchError("SUPPLEMENT_LINKED_TO_WRONG_TICKET")
            sent_rma_replies = [
                row
                for row in detail.get("reply_records", [])
                if row.get("reply_type") == "rma_authorization"
                and row.get("send_status") == "sent"
            ]
            if len(sent_rma_replies) != 1:
                raise BatchError("SUPPLEMENT_RMA_REPLY_COUNT_INVALID")
            reply = sent_rma_replies[0]
            if (
                not exact_recipient(reply)
                or not test_only_subject(
                    reply.get("subject")
                ).upper().startswith("[TEST ONLY]")
                or reply.get("in_reply_to") != supplement_message_id
                or supplement_message_id
                not in str(reply.get("references_header") or "")
                or original_message_id
                not in str(reply.get("references_header") or "")
                or not reply.get("rma_pdf_oss_object_id")
            ):
                raise BatchError(
                    "SUPPLEMENT_RMA_ENVELOPE_THREAD_OR_PDF_INVALID"
                )
            original_state.update(
                {
                    "final_status": ticket.get("current_status_code"),
                    "awaiting_customer_supplement": False,
                    "supplement_email_id": supplement_email_id,
                    "supplement_message_id": supplement_message_id,
                    "rma_reply_id": reply.get("id"),
                    "rma_reply_message_id": reply.get("smtp_message_id"),
                    "rma_pdf_oss_object_id": reply.get(
                        "rma_pdf_oss_object_id"
                    ),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            state["actual_send_count"] = int(
                state.get("actual_send_count") or 0
            ) + 1
            _write_json(state_path, state)
        else:
            raise BatchError("EXECUTION_PHASE_INVALID")
        return {
            "status": "phase_complete",
            "phase": phase,
            "processed_count": len(state["messages"]),
            "actual_send_count": state["actual_send_count"],
            "state_path": str(state_path),
            "initial_runtime_config": initial,
        }
    finally:
        patch_config(
            client,
            auto_send_enabled=False,
            auto_followup_enabled=False,
            rma_auto_send_enabled=True,
        )


def relay_reset(base_url: str, token: str) -> None:
    request = Request(
        f"{base_url.rstrip('/')}/control/reset",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise BatchError("TEST_RELAY_RESET_FAILED")


def write_report(manifest_path: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent
    evidence_files = sorted(
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and path != manifest_path
    )
    lines = [
        f"# rmatest1 真实邮件全链路测试报告（{manifest['batch_id']}）",
        "",
        "## 当前结论",
        "",
        "- 当前阶段：只读冻结批次，等待业务金标确认。",
        "- 实际发送数量：0。",
        "- 未执行邮件解析、建单、中转提交、RMA 生成或回复。",
        "",
        "## 冻结批次",
        "",
        f"- 邮件数量：{len(manifest.get('messages', []))}",
        f"- UIDVALIDITY：`{manifest.get('uid_validity')}`",
        f"- 冻结 UID 上限：`{manifest.get('frozen_uid_max')}`",
        f"- 金标批准：`{bool(manifest.get('business_gold_approved'))}`",
        "",
        "报告不包含邮件全文、附件内容、完整个人信息、密码、Token 或 OSS 签名 URL。",
        "",
        "## 本地证据",
        "",
        *[f"- `{name}`" for name in evidence_files],
        "",
    ]
    report = Path(__file__).parents[2] / "docs" / "08-rmatest1真实邮件全链路测试报告-20260729.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="rmatest1 gold-manifest batch test orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory_parser = subparsers.add_parser("inventory-only")
    inventory_parser.add_argument("--batch-id", required=True)
    inventory_parser.add_argument("--limit", type=int, default=100)
    inventory_parser.add_argument("--since-uid", type=int)
    inventory_parser.add_argument("--include-seen", action="store_true")
    inventory_parser.add_argument("--exclude-manifest", type=Path)
    validate_parser = subparsers.add_parser("validate-manifest")
    validate_parser.add_argument("--manifest", type=Path, required=True)
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--manifest", type=Path, required=True)
    execute_parser.add_argument(
        "--phase",
        choices=[
            "prepare",
            "master-data",
            "classify",
            "canary",
            "recover-auto-rma",
            "auto-rma",
            "auto-followup",
            "supplement-rma",
        ],
        default="prepare",
    )
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--manifest", type=Path, required=True)
    resume_parser.add_argument(
        "--phase",
        choices=[
            "prepare",
            "master-data",
            "classify",
            "canary",
            "recover-auto-rma",
            "auto-rma",
            "auto-followup",
            "supplement-rma",
        ],
        default="prepare",
    )
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--manifest", type=Path, required=True)
    cleanup_parser.add_argument("--relay-url")
    cleanup_parser.add_argument("--relay-token")
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "inventory-only":
        print(inventory(
            args.batch_id,
            limit=args.limit,
            since_uid=args.since_uid,
            include_seen=args.include_seen,
            exclude_manifest=args.exclude_manifest,
        ))
    elif args.command == "validate-manifest":
        print(json.dumps(validate_manifest(args.manifest), ensure_ascii=False))
    elif args.command in {"execute", "resume"}:
        print(json.dumps(
            execute_phase(
                args.manifest,
                phase=args.phase,
                resume=args.command == "resume",
            ),
            ensure_ascii=False,
            default=str,
        ))
    elif args.command == "cleanup":
        cleanup = asyncio.run(cleanup_temporary_master_data(args.manifest))
        if args.relay_url and args.relay_token:
            relay_reset(args.relay_url, args.relay_token)
        print(json.dumps({"status": "cleaned", **cleanup}, ensure_ascii=False))
    elif args.command == "report":
        print(write_report(args.manifest))


if __name__ == "__main__":
    main()
