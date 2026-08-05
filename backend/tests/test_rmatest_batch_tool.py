import json
from pathlib import Path

import pytest

from tools.run_rmatest_batch_e2e import (
    BatchError,
    RECOVERY_READY_OR_COMPLETED_STATUSES,
    _mail_gate,
    validate_manifest,
)


def _manifest() -> dict:
    return {
        "schema_version": 2,
        "mailbox": "rmatest1@accotest.com",
        "recipient_only": "rmatest2@accotest.com",
        "business_gold_approved": True,
        "approved_by": "tester",
        "approved_at": "2026-07-29T10:00:00+08:00",
        "max_actual_sends": 1,
        "messages": [
            {
                "uid": "101",
                "header_sha256": "a" * 64,
                "message_id": "<case-1@accotest.com>",
                "gold": {
                    "expected_intent": "irrelevant",
                    "expected_subtype": "out_of_scope_repair",
                    "create_ticket": False,
                    "reply_allowed": False,
                    "send_mode": "none",
                    "expected_outbound_count": 0,
                    "expected_final_status": "OUT_OF_SCOPE_REPAIR",
                    "expected_manual_action": "none",
                },
            },
            {
                "uid": "102",
                "header_sha256": "b" * 64,
                "message_id": "<case-2@accotest.com>",
                "gold": {
                    "expected_intent": "new_repair",
                    "expected_subtype": None,
                    "create_ticket": True,
                    "reply_allowed": True,
                    "send_mode": "auto_canary",
                    "expected_outbound_count": 1,
                    "expected_final_status": "closed",
                    "expected_rma_status": "issued",
                    "expected_manual_action": "approve RMA",
                },
            },
        ],
    }


def test_manifest_validation_enforces_send_cap_and_single_canary(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    result = validate_manifest(path, require_approval=True)
    assert result["valid"] is True
    assert result["planned_sends"] == 1
    assert result["auto_canaries"] == 1


def test_recovery_accepts_closed_after_pending_rma_was_sent() -> None:
    assert "closed" in RECOVERY_READY_OR_COMPLETED_STATUSES


def test_manifest_requires_irrelevant_subtype(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["messages"][0]["gold"]["expected_subtype"] = None
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BatchError, match="irrelevant_subtype_REQUIRED"):
        validate_manifest(path)


def test_manifest_rejects_stale_rma_sent_canary_contract(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["messages"][1]["gold"]["expected_final_status"] = "rma_sent"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BatchError, match="auto_canary_MUST_BE_COMPLETE_NEW_REPAIR"):
        validate_manifest(path)


def test_manifest_requires_frozen_uid_and_header_hash(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["messages"][1].pop("uid")
    manifest["messages"][1]["header_sha256"] = "not-a-hash"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BatchError, match="uid_REQUIRED") as exc_info:
        validate_manifest(path)
    assert "header_sha256_INVALID" in str(exc_info.value)


def test_mail_gate_uses_persisted_runtime_config(monkeypatch) -> None:
    monkeypatch.setattr(
        "tools.run_rmatest_batch_e2e.test_mail_configuration_reasons", lambda: []
    )
    monkeypatch.setattr(
        "tools.run_rmatest_batch_e2e.read_runtime_config",
        lambda: {"auto_send_enabled": True, "auto_followup_enabled": False},
    )
    with pytest.raises(BatchError, match="ALL_AUTO_SEND_DISABLED"):
        _mail_gate()
