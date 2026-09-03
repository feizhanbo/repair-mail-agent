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
    RepairTicket,
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
    exact_test_transport_subject,
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
RECOVERY_READY_OR_COMPLETED_STATUSES = {"ready_for_export", "rma_sent", "closed"}
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
                        "temporary_customer_policies": [],
                        "expected_final_status": None,
                        "expected_rma_status": None,
                        "expected_manual_action": None,
                    },
                }
            )
        frozen = [str(item["uid"]).encode("ascii") for item in messages]
        manifest = {
            "schema_version": 2,
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
    if manifest.get("schema_version") != 2:
        errors.append("SCHEMA_VERSION_MUST_EQUAL_2")
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
        if not str(item.get("uid") or "").isdigit():
            errors.append(f"{prefix}.uid_REQUIRED")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("header_sha256") or "")):
            errors.append(f"{prefix}.header_sha256_INVALID")
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
        if not isinstance(gold.get("expected_fields", {}), dict):
            errors.append(f"{prefix}.expected_fields_INVALID")
        if not isinstance(gold.get("expected_items", []), list):
            errors.append(f"{prefix}.expected_items_INVALID")
        if not isinstance(gold.get("missing_fields", []), list):
            errors.append(f"{prefix}.missing_fields_INVALID")
        if gold.get("reply_allowed") is False and expected_outbound_count:
            errors.append(f"{prefix}.REPLY_FORBIDDEN_BUT_SEND_PLANNED")
        if mode == "none" and expected_outbound_count:
            errors.append(f"{prefix}.SEND_MODE_NONE_BUT_SEND_PLANNED")
        if mode == "auto_canary":
            auto_canaries += 1
            if (
                intent != "new_repair"
                or gold.get("expected_final_status") != "rma_sent"
                or gold.get("expected_rma_status") != "issued"
                or expected_outbound_count != 1
            ):
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
    board_rows: dict[tuple[str, str], dict[str, Any]] = {}
    policy_rows: dict[str, dict[str, Any]] = {}
    for message in manifest["messages"]:
        gold = message["gold"]
        for row in gold.get("temporary_sn_assets") or []:
            sn = str(row.get("sn") or "").strip().upper()
            if not sn:
                raise BatchError("TEMPORARY_SN_REQUIRES_SN")
            normalized_row = dict(row)
            normalized_row["sn"] = sn
            # SAP export now requires the source-system primary key. Older
            # reusable gold manifests predate that field, so provide a stable,
            # test-only integer rather than weakening the production safety
            # gate or making every approved manifest churn.
            normalized_row.setdefault(
                "ins_id",
                1_000_000_000 + int(hashlib.sha256(sn.encode("utf-8")).hexdigest()[:7], 16),
            )
            if sn in sn_rows and sn_rows[sn] != normalized_row:
                raise BatchError(f"TEMPORARY_SN_CONFLICT:{sn}")
            sn_rows[sn] = normalized_row
        for row in gold.get("temporary_board_cards") or []:
            material = str(row.get("material_code") or "").strip()
            board_code = str(row.get("board_code") or "").strip()
            if not material:
                raise BatchError("TEMPORARY_BOARD_CARD_REQUIRES_MATERIAL_CODE")
            key = (material, board_code)
            if key in board_rows and board_rows[key] != row:
                raise BatchError(f"TEMPORARY_BOARD_CARD_CONFLICT:{material}:{board_code}")
            board_rows[key] = row
        for row in gold.get("temporary_customer_policies") or []:
            policy_code = str(row.get("policy_code") or "").strip()
            if not policy_code:
                raise BatchError("TEMPORARY_CUSTOMER_POLICY_REQUIRES_POLICY_CODE")
            if policy_code in policy_rows and policy_rows[policy_code] != row:
                raise BatchError(f"TEMPORARY_CUSTOMER_POLICY_CONFLICT:{policy_code}")
            policy_rows[policy_code] = row
    return list(sn_rows.values()), list(board_rows.values()), list(policy_rows.values())


def _fresh_temporary_master_state(
    state_path: Path, *, batch_id: str
) -> dict[str, Any]:
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {"batch_id": batch_id, "messages": {}, "actual_send_count": 0}
    )
    completed_cleanup = state.get("cleanup")
    if isinstance(completed_cleanup, dict) and completed_cleanup.get("completed_at"):
        return {"batch_id": batch_id, "messages": {}, "actual_send_count": 0}
    return state


async def apply_temporary_master_data(
    manifest: dict[str, Any],
    state_path: Path,
    *,
    allow_gold_e2e_snapshot_override: bool = False,
) -> dict[str, Any]:
    sn_rows, board_rows, policy_rows = _temporary_master_rows(manifest)
    batch_id = str(manifest["batch_id"])
    source_hash = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()
    state = _fresh_temporary_master_state(state_path, batch_id=batch_id)
    # A reusable gold suite can apply the same fixture again after a prior
    # cleanup.  Reusing the old completed marker would make the next cleanup
    # return early and leak freshly-created master data.
    created = state.setdefault(
        "temporary_master_data",
        {"sn_asset_ids": [], "board_card_ids": [], "customer_policy_ids": []},
    )
    created.setdefault("customer_policy_ids", [])
    overridden_sn_assets = created.setdefault("overridden_sn_assets", [])
    overridden_board_cards = created.setdefault("overridden_board_cards", [])
    async with AsyncSessionLocal() as session:
        for row_no, row in enumerate(sn_rows, 1):
            sn = str(row["sn"]).strip().upper()
            existing = await session.scalar(select(SnAsset).where(SnAsset.sn == sn))
            if existing is not None:
                if existing.source_file_name == batch_id and existing.id in created["sn_asset_ids"]:
                    existing.ins_id = int(row["ins_id"])
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
                existing_snapshot = next(
                    (
                        snapshot
                        for snapshot in overridden_sn_assets
                        if int(snapshot.get("id") or 0) == existing.id
                    ),
                    None,
                )
                raw_data = existing.raw_data if isinstance(existing.raw_data, dict) else {}
                if not (
                    allow_gold_e2e_snapshot_override
                    and (
                        existing_snapshot is not None
                        or (
                            existing.source_system == "e2e_test"
                            and raw_data.get("gold_confirmed") is True
                        )
                    )
                ):
                    raise BatchError(f"TEMPORARY_SN_ALREADY_EXISTS:{sn}")
                required = ("customer_code", "customer_name", "material_code")
                missing = [
                    field for field in required if not str(row.get(field) or "").strip()
                ]
                if missing:
                    raise BatchError(
                        f"TEMPORARY_SN_FIELDS_REQUIRED:{sn}:{','.join(missing)}"
                    )
                if existing_snapshot is None:
                    overridden_sn_assets.append(
                        {
                            "id": existing.id,
                            "sn": existing.sn,
                            "ins_id": existing.ins_id,
                            "customer_code": existing.customer_code,
                            "customer_name": existing.customer_name,
                            "material_code": existing.material_code,
                            "material_name": existing.material_name,
                            "asset_status": existing.asset_status,
                            "warranty_start_date": existing.warranty_start_date,
                            "warranty_end_date": existing.warranty_end_date,
                            "source_file_name": existing.source_file_name,
                            "source_file_hash": existing.source_file_hash,
                            "source_row_no": existing.source_row_no,
                            "source_system": existing.source_system,
                            "external_id": existing.external_id,
                            "source_updated_at": existing.source_updated_at,
                            "raw_data": existing.raw_data,
                        }
                    )
                existing.customer_code = str(row["customer_code"]).strip()
                existing.ins_id = int(row["ins_id"])
                existing.customer_name = str(row["customer_name"]).strip()
                existing.material_code = str(row["material_code"]).strip()
                existing.material_name = (
                    str(row.get("material_name") or "").strip() or None
                )
                existing.asset_status = "valid"
                existing.warranty_start_date = _optional_date(
                    row.get("warranty_start_date")
                )
                existing.warranty_end_date = _optional_date(
                    row.get("warranty_end_date")
                )
                existing.source_file_name = batch_id
                existing.source_file_hash = source_hash
                existing.source_row_no = row_no
                existing.source_system = "e2e_test"
                existing.external_id = None
                existing.source_updated_at = None
                existing.raw_data = {
                    "batch_id": batch_id,
                    "gold_confirmed": True,
                    "temporarily_overrode_e2e_gold": True,
                }
                continue
            required = ("customer_code", "customer_name", "material_code")
            missing = [field for field in required if not str(row.get(field) or "").strip()]
            if missing:
                raise BatchError(f"TEMPORARY_SN_FIELDS_REQUIRED:{sn}:{','.join(missing)}")
            asset = SnAsset(
                ins_id=int(row["ins_id"]),
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
            board_code = str(row.get("board_code") or "").strip()
            existing = await session.scalar(
                select(BoardCard).where(
                    BoardCard.material_code == material,
                    BoardCard.board_code == board_code,
                )
            )
            if existing is not None:
                if existing.source_file_name == batch_id and existing.id in created["board_card_ids"]:
                    continue
                existing_snapshot = next(
                    (
                        snapshot
                        for snapshot in overridden_board_cards
                        if int(snapshot.get("id") or 0) == existing.id
                    ),
                    None,
                )
                raw_data = existing.raw_data if isinstance(existing.raw_data, dict) else {}
                if not (
                    allow_gold_e2e_snapshot_override
                    and (
                        existing_snapshot is not None
                        or (
                            existing.source_file_name
                            in {"controlled-mail-e2e", "e2e_test"}
                            and (
                                raw_data.get("gold_confirmed") is True
                                or raw_data.get("purpose") == "controlled_mail_e2e"
                            )
                        )
                    )
                ):
                    raise BatchError(f"TEMPORARY_BOARD_CARD_ALREADY_EXISTS:{material}")
                if existing_snapshot is None:
                    overridden_board_cards.append(
                        {
                            "id": existing.id,
                            "board_code": existing.board_code,
                            "board_name": existing.board_name,
                            "return_location": existing.return_location,
                            "route_type": existing.route_type,
                            "customer_scope": existing.customer_scope,
                            "material_code": existing.material_code,
                            "material_name": existing.material_name,
                            "need_ship_to_beijing": existing.need_ship_to_beijing,
                            "shipping_address": existing.shipping_address,
                            "shipping_contact": existing.shipping_contact,
                            "shipping_phone": existing.shipping_phone,
                            "postal_code": existing.postal_code,
                            "status": existing.status,
                            "source_file_name": existing.source_file_name,
                            "source_file_hash": existing.source_file_hash,
                            "source_row_no": existing.source_row_no,
                            "raw_data": existing.raw_data,
                        }
                    )
                required = (
                    "board_code",
                    "return_location",
                    "route_type",
                    "customer_scope",
                    "shipping_address",
                    "shipping_contact",
                    "shipping_phone",
                )
                missing = [
                    field for field in required if not str(row.get(field) or "").strip()
                ]
                if missing:
                    raise BatchError(
                        "TEMPORARY_BOARD_CARD_FIELDS_REQUIRED:"
                        + material
                        + ":"
                        + ",".join(missing)
                    )
                existing.board_code = str(row["board_code"]).strip()
                existing.board_name = str(row.get("board_name") or "").strip() or None
                existing.return_location = str(row["return_location"]).strip()
                existing.route_type = str(row["route_type"]).strip()
                existing.customer_scope = str(row["customer_scope"]).strip()
                existing.material_name = str(row.get("material_name") or "").strip() or None
                existing.need_ship_to_beijing = bool(row.get("need_ship_to_beijing", True))
                existing.shipping_address = str(row["shipping_address"]).strip()
                existing.shipping_contact = str(row["shipping_contact"]).strip()
                existing.shipping_phone = str(row["shipping_phone"]).strip()
                existing.postal_code = str(row.get("postal_code") or "").strip() or None
                existing.status = "active"
                existing.source_file_name = batch_id
                existing.source_file_hash = source_hash
                existing.source_row_no = row_no
                existing.raw_data = {
                    "batch_id": batch_id,
                    "gold_confirmed": True,
                    "temporarily_overrode_e2e_gold": True,
                }
                continue
            required = (
                "board_code",
                "return_location",
                "route_type",
                "customer_scope",
                "shipping_address",
                "shipping_contact",
                "shipping_phone",
            )
            missing = [field for field in required if not str(row.get(field) or "").strip()]
            if missing:
                raise BatchError(
                    "TEMPORARY_BOARD_CARD_FIELDS_REQUIRED:"
                    + material
                    + ":"
                    + ",".join(missing)
                )
            card = BoardCard(
                board_code=str(row["board_code"]).strip(),
                board_name=str(row.get("board_name") or "").strip() or None,
                return_location=str(row["return_location"]).strip(),
                route_type=str(row["route_type"]).strip(),
                customer_scope=str(row["customer_scope"]).strip(),
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
                "charge_status",
                "customer_scope",
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
                charge_status=str(row["charge_status"]).strip(),
                customer_scope=str(row["customer_scope"]).strip(),
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


async def cleanup_temporary_master_data(
    manifest_path: Path,
    *,
    state_path: Path | None = None,
    skip_manifest_validation: bool = False,
) -> dict[str, Any]:
    if not skip_manifest_validation:
        validate_manifest(manifest_path, require_approval=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state_path = state_path or manifest_path.parent / "execution-state.json"
    if not state_path.exists():
        return {
            "sn_assets_deleted": 0,
            "board_cards_deleted": 0,
            "customer_policies_deleted": 0,
            "sn_assets_restored": 0,
            "board_cards_restored": 0,
            "sn_assets_planned": 0,
            "board_cards_planned": 0,
            "customer_policies_planned": 0,
            "sn_assets_restore_planned": 0,
            "board_cards_restore_planned": 0,
            "this_run": {
                "sn_assets_deleted": 0,
                "board_cards_deleted": 0,
                "customer_policies_deleted": 0,
                "sn_assets_restored": 0,
                "board_cards_restored": 0,
            },
        }
    state = json.loads(state_path.read_text(encoding="utf-8"))
    completed_cleanup = state.get("cleanup")
    created = state.get("temporary_master_data") or {}
    sn_ids = [int(value) for value in created.get("sn_asset_ids") or []]
    board_ids = [int(value) for value in created.get("board_card_ids") or []]
    policy_ids = [int(value) for value in created.get("customer_policy_ids") or []]
    overridden_sn_assets = list(created.get("overridden_sn_assets") or [])
    overridden_board_cards = list(created.get("overridden_board_cards") or [])
    batch_id = str(manifest["batch_id"])
    actual_counts = {
        "sn_assets_deleted": 0,
        "board_cards_deleted": 0,
        "customer_policies_deleted": 0,
        "sn_assets_restored": 0,
        "board_cards_restored": 0,
    }
    async with AsyncSessionLocal() as session:
        if isinstance(completed_cleanup, dict) and completed_cleanup.get("completed_at"):
            pending_batch_rows = False
            checks = (
                (SnAsset, sn_ids),
                (BoardCard, board_ids),
                (CustomerServicePolicy, policy_ids),
            )
            for model, ids in checks:
                if ids and await session.scalar(
                    select(model.id).where(
                        model.id.in_(ids), model.source_file_name == batch_id
                    ).limit(1)
                ):
                    pending_batch_rows = True
                    break
            if not pending_batch_rows:
                for model, snapshots in (
                    (SnAsset, overridden_sn_assets),
                    (BoardCard, overridden_board_cards),
                ):
                    snapshot_ids = [
                        int(snapshot.get("id") or 0) for snapshot in snapshots
                    ]
                    if snapshot_ids and await session.scalar(
                        select(model.id).where(
                            model.id.in_(snapshot_ids),
                            model.source_file_name == batch_id,
                        ).limit(1)
                    ):
                        pending_batch_rows = True
                        break
            if not pending_batch_rows:
                return {
                    **completed_cleanup,
                    "already_completed": True,
                    "this_run": {key: 0 for key in actual_counts},
                }
        if sn_ids:
            assets = (
                await session.execute(select(SnAsset).where(SnAsset.id.in_(sn_ids)))
            ).scalars().all()
            if any(asset.source_file_name != batch_id for asset in assets):
                raise BatchError("TEMPORARY_SN_CLEANUP_SCOPE_MISMATCH")
            actual_counts["sn_assets_deleted"] = len(assets)
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
            actual_counts["board_cards_deleted"] = len(cards)
            await session.execute(
                update(RepairTicketItem)
                .where(RepairTicketItem.matched_board_card_id.in_(board_ids))
                .values(matched_board_card_id=None)
            )
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
            actual_counts["customer_policies_deleted"] = len(policies)
            await session.execute(
                update(RepairTicket)
                .where(RepairTicket.service_policy_id.in_(policy_ids))
                .values(service_policy_id=None)
            )
            await session.execute(
                delete(CustomerServicePolicy).where(
                    CustomerServicePolicy.id.in_(policy_ids)
                )
            )
        for snapshot in overridden_sn_assets:
            asset = await session.get(SnAsset, int(snapshot["id"]))
            if asset is None or asset.sn != snapshot.get("sn"):
                raise BatchError("TEMPORARY_SN_RESTORE_SCOPE_MISMATCH")
            if asset.source_file_name != batch_id:
                restored_fields = (
                    "ins_id",
                    "customer_code",
                    "customer_name",
                    "material_code",
                    "material_name",
                    "asset_status",
                    "source_file_name",
                    "source_file_hash",
                    "source_row_no",
                    "source_system",
                    "external_id",
                    "raw_data",
                )
                already_restored = all(
                    getattr(asset, field) == snapshot.get(field)
                    for field in restored_fields
                )
                if not already_restored:
                    raise BatchError("TEMPORARY_SN_RESTORE_SOURCE_MISMATCH")
                continue
            for field in (
                "customer_code",
                "ins_id",
                "customer_name",
                "material_code",
                "material_name",
                "asset_status",
                "source_file_name",
                "source_file_hash",
                "source_row_no",
                "source_system",
                "external_id",
                "raw_data",
            ):
                setattr(asset, field, snapshot.get(field))
            actual_counts["sn_assets_restored"] += 1
            asset.warranty_start_date = _optional_date(
                snapshot.get("warranty_start_date")
            )
            asset.warranty_end_date = _optional_date(snapshot.get("warranty_end_date"))
            source_updated_at = snapshot.get("source_updated_at")
            asset.source_updated_at = (
                datetime.fromisoformat(source_updated_at)
                if isinstance(source_updated_at, str) and source_updated_at
                else source_updated_at
            )
        for snapshot in overridden_board_cards:
            card = await session.get(BoardCard, int(snapshot["id"]))
            if card is None or card.material_code != snapshot.get("material_code"):
                raise BatchError("TEMPORARY_BOARD_RESTORE_SCOPE_MISMATCH")
            if card.source_file_name != batch_id:
                restored_fields = (
                    "board_code",
                    "board_name",
                    "return_location",
                    "route_type",
                    "customer_scope",
                    "material_code",
                    "material_name",
                    "need_ship_to_beijing",
                    "shipping_address",
                    "shipping_contact",
                    "shipping_phone",
                    "postal_code",
                    "status",
                    "source_file_name",
                    "source_file_hash",
                    "source_row_no",
                    "raw_data",
                )
                already_restored = all(
                    getattr(card, field) == snapshot.get(field)
                    for field in restored_fields
                )
                if not already_restored:
                    raise BatchError("TEMPORARY_BOARD_RESTORE_SOURCE_MISMATCH")
                continue
            for field in (
                "board_code",
                "board_name",
                "return_location",
                "route_type",
                "customer_scope",
                "material_code",
                "material_name",
                "need_ship_to_beijing",
                "shipping_address",
                "shipping_contact",
                "shipping_phone",
                "postal_code",
                "status",
                "source_file_name",
                "source_file_hash",
                "source_row_no",
                "raw_data",
            ):
                setattr(card, field, snapshot.get(field))
            actual_counts["board_cards_restored"] += 1
        await session.commit()
    state["cleanup"] = {
        **actual_counts,
        "sn_assets_planned": len(sn_ids),
        "board_cards_planned": len(board_ids),
        "customer_policies_planned": len(policy_ids),
        "sn_assets_restore_planned": len(overridden_sn_assets),
        "board_cards_restore_planned": len(overridden_board_cards),
        "this_run": dict(actual_counts),
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
                auto_followup_enabled=True,
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
                # The prior attempt may have completed SMTP + archive but
                # crashed before persisting its counter.  This branch is only
                # entered for a message absent from state and exactly one
                # verified sent RMA, so reconcile that real outbound once.
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
                    if post_ticket.get("current_status_code") not in RECOVERY_READY_OR_COMPLETED_STATUSES:
                        raise BatchError(
                            "RECOVERY_TICKET_NOT_READY_FOR_EXPORT"
                        )
            elif ticket.get("current_status_code") not in RECOVERY_READY_OR_COMPLETED_STATUSES:
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
            expected_mode = "auto_canary" if phase == "canary" else "auto_rma"
            candidates = [
                item for item in manifest["messages"]
                if item["gold"]["send_mode"] == expected_mode
                and item["message_id"] not in state["messages"]
            ]
            if not candidates:
                raise BatchError("NO_PENDING_AUTO_RMA_MESSAGE")
            target = candidates[0]
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
                auto_followup_enabled=True,
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
                auto_followup_enabled=True,
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
                or not exact_test_transport_subject(detail, reply)
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
            auto_send_enabled=bool(initial.get("auto_send_enabled")),
            auto_followup_enabled=bool(initial.get("auto_followup_enabled")),
            rma_auto_send_enabled=bool(initial.get("rma_auto_send_enabled")),
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
    state_path = root / "execution-state.json"
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {"messages": {}, "actual_send_count": 0}
    )
    evidence_files = sorted(
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and path != manifest_path
    )
    batch_id = str(manifest.get("batch_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,100}", batch_id):
        raise BatchError("BATCH_ID_INVALID_FOR_REPORT")
    message_lines: list[str] = []
    for item in manifest.get("messages") or []:
        message_id = str(item.get("message_id") or "")
        gold = item.get("gold") or {}
        actual = (state.get("messages") or {}).get(message_id) or {}
        message_lines.extend(
            [
                f"### `{message_id}`",
                "",
                f"- 金标意图：`{gold.get('expected_intent')}`",
                f"- 预期最终状态：`{gold.get('expected_final_status')}`",
                f"- 实际最终状态：`{actual.get('final_status') or actual.get('parse_status') or '未执行'}`",
                f"- 工单 ID：`{actual.get('ticket_id') or '无'}`",
                f"- 回复 ID：`{actual.get('reply_id') or actual.get('rma_reply_id') or '无'}`",
                "",
            ]
        )
    report_status = "已执行" if state_path.exists() else "仅完成批次冻结"
    lines = [
        f"# rmatest1 单封金标邮件全链路复测报告（{batch_id}）",
        "",
        "## 执行摘要",
        "",
        f"- 当前阶段：{report_status}",
        f"- 实际发送数量：`{int(state.get('actual_send_count') or 0)}`",
        f"- 计划发送上限：`{int(manifest.get('max_actual_sends') or 0)}`",
        f"- 金标批准：`{bool(manifest.get('business_gold_approved'))}`",
        "",
        "## 冻结批次",
        "",
        f"- 邮件数量：`{len(manifest.get('messages', []))}`",
        f"- UIDVALIDITY：`{manifest.get('uid_validity')}`",
        f"- 冻结 UID 上限：`{manifest.get('frozen_uid_max')}`",
        "",
        "## 逐封结果",
        "",
        *message_lines,
        "## 安全与隐私",
        "",
        "报告不包含邮件全文、附件内容、完整个人信息、密码、Token 或 OSS 签名 URL。",
        "",
        "## 本地证据",
        "",
        *[f"- `{name}`" for name in evidence_files],
        "",
    ]
    report_date = datetime.now().astimezone().strftime("%Y%m%d")
    report = (
        Path(__file__).parents[2]
        / "docs"
        / f"08-rmatest1单封金标邮件全链路复测报告-{report_date}.md"
    )
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
