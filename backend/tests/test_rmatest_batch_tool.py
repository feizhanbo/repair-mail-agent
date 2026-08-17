import json
from pathlib import Path

import pytest

from tools.run_rmatest_batch_e2e import (
    BatchError,
    RECOVERY_READY_OR_COMPLETED_STATUSES,
    _mail_gate,
    _fresh_temporary_master_state,
    _temporary_master_rows,
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
                    "expected_final_status": "rma_sent",
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


def test_manifest_rejects_stale_closed_canary_contract(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["messages"][1]["gold"]["expected_final_status"] = "closed"
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


def test_temporary_board_rows_keep_distinct_board_codes_for_same_material() -> None:
    manifest = {
        "messages": [
            {
                "gold": {
                    "temporary_sn_assets": [],
                    "temporary_customer_policies": [],
                    "temporary_board_cards": [
                        {"material_code": "ROUTE-X", "board_code": "FOVI"},
                        {"material_code": "ROUTE-X", "board_code": "DIO"},
                    ],
                }
            }
        ]
    }

    _, rows, _ = _temporary_master_rows(manifest)

    assert {row["board_code"] for row in rows} == {"FOVI", "DIO"}


def test_completed_fixture_state_starts_a_new_cleanup_cycle(tmp_path: Path) -> None:
    state_path = tmp_path / "temporary-master-state.json"
    state_path.write_text(
        json.dumps(
            {
                "batch_id": "gold-batch",
                "temporary_master_data": {"board_card_ids": [123]},
                "cleanup": {"completed_at": "2026-08-12T10:00:00+00:00"},
            }
        ),
        encoding="utf-8",
    )

    state = _fresh_temporary_master_state(state_path, batch_id="gold-batch")

    assert state == {
        "batch_id": "gold-batch",
        "messages": {},
        "actual_send_count": 0,
    }
