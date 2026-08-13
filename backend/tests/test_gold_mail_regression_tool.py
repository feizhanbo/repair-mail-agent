from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.services import gold_replay
from tools import run_gold_mail_regression as tool
from tools.test_relay_server import RelayControl, RelayRecord, TestRelayStore


def _manifest(tmp_path: Path) -> Path:
    message_id = "<gold-001@accotest.com>"
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "suite_id": "suite-001",
                "source_mailbox": "rmatest1@accotest.com",
                "outbound_recipient_only": "rmatest2@accotest.com",
                "max_actual_sends": 1,
                "messages": [
                    {
                        "uid": "101",
                        "uid_validity": "9",
                        "message_id": message_id,
                        "raw_sha256": hashlib.sha256(b"gold-eml").hexdigest(),
                        "gold": {
                            "expected_intent": "new_repair",
                            "expected_subtype": None,
                            "expected_fields": {"customer_code": "CM00001"},
                            "expected_items": [{"sn": "SN-GOLD-001"}],
                            "missing_fields": [],
                            "create_ticket": True,
                            "expected_final_status": "rma_sent",
                            "expected_outbound_count": 1,
                            "send_mode": "auto_rma",
                            "fixed_rma_no": "2026081201",
                            "temporary_sn_assets": [],
                            "temporary_board_cards": [],
                            "temporary_customer_policies": [],
                            "supplement": None,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_manifest_approval_is_bound_to_exact_sha256(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    assert tool.validate_manifest(manifest)["valid"] is True
    approved = tool.approve_manifest(manifest, "business-owner", True)
    assert approved["status"] == "approved"
    assert tool.validate_manifest(manifest, require_approval=True)["approved"] is True

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["messages"][0]["gold"]["expected_fields"]["customer_code"] = "CHANGED"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(tool.GoldCliError) as exc:
        tool.validate_manifest(manifest, require_approval=True)
    assert exc.value.code == "MANIFEST_INVALID"
    assert "UNCHANGED_MANIFEST_APPROVAL_REQUIRED" in exc.value.details["errors"]


def test_manifest_rejects_invalid_calendar_rma(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["messages"][0]["gold"]["fixed_rma_no"] = "2026023001"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(tool.GoldCliError) as exc:
        tool.validate_manifest(manifest)
    assert "messages[0].fixed_rma_no_INVALID" in exc.value.details["errors"]


def test_doctor_returns_stable_blocked_result_without_database_traceback(monkeypatch) -> None:
    async def blocked_database() -> dict:
        return {"passed": False, "detail": {"code": "DATABASE_CONNECTION_FAILED"}}

    monkeypatch.setattr(tool, "_database_doctor", blocked_database)
    result = tool.doctor(live=False)
    assert result["status"] == "blocked"
    assert result["secrets_exposed"] is False
    assert any(row["name"] == "database_and_relay_gate" and not row["passed"] for row in result["checks"])


def test_relay_default_fixed_rma_is_idempotent_for_multiple_sn(tmp_path: Path) -> None:
    store = TestRelayStore(tmp_path / "fixed-rma.sqlite3")
    store.configure(RelayControl(scenario="normal", rma_no="2026081201"))
    first = RelayRecord(source_request_id="request-fixed-0001", ticket_id=9, ticket_item_id=1, sn="SN-1")
    second = RelayRecord(source_request_id="request-fixed-0002", ticket_id=9, ticket_item_id=2, sn="SN-2")
    first_result = store.create(first)
    store.create(second)
    rows = store.query(["request-fixed-0001", "request-fixed-0002"])
    assert {row["rma_no"] for row in rows} == {"2026081201"}
    assert store.create(first)["remote_record_key"] == first_result["remote_record_key"]
    assert store.create(first)["idempotent_reuse"] is True


def test_cleanup_apply_requires_preview_plan_hash(tmp_path: Path) -> None:
    parser = tool.build_parser()
    args = parser.parse_args(["cleanup", "--manifest", str(_manifest(tmp_path)), "--apply"])
    assert args.apply is True
    assert args.plan_hash is None


def test_langgraph_gold_evidence_requires_v2_checkpoint_and_stable_status(monkeypatch) -> None:
    monkeypatch.setattr(
        tool,
        "settings",
        Settings(
            _env_file=None,
            WORKFLOW_ENGINE="langgraph",
            LANGGRAPH_CHECKPOINT_DATABASE_URL="postgresql://unused/graph",
        ),
    )
    valid = {
        "found": True,
        "workflow_version": "langgraph-v2",
        "execution_mode": "langgraph",
        "status": "completed",
        "checkpoint_present": True,
        "checkpoint_step": 12,
        "last_error_code": None,
        "interrupts": [
            {
                "status": "resumed",
                "checkpoint_present": True,
                "checkpoint_step": 8,
                "error_present": False,
            }
        ],
    }
    assert tool._assert_graph_execution_evidence(valid) == []

    invalid = {
        **valid,
        "workflow_version": "langgraph-v1",
        "status": "failed",
        "checkpoint_present": False,
        "checkpoint_step": None,
        "last_error_code": "WORKFLOW_CHECKPOINT_MISSING",
        "interrupts": [
            {
                "status": "pending",
                "checkpoint_present": False,
                "checkpoint_step": None,
                "error_present": True,
            }
        ],
    }
    assert set(tool._assert_graph_execution_evidence(invalid)) == {
        "LANGGRAPH_WORKFLOW_VERSION_MISMATCH",
        "LANGGRAPH_EXECUTION_CHECKPOINT_MISSING",
        "LANGGRAPH_EXECUTION_NOT_STABLE",
        "LANGGRAPH_EXECUTION_HAS_ERROR",
        "LANGGRAPH_INTERRUPT_CHECKPOINT_MISSING",
        "LANGGRAPH_INTERRUPT_HAS_ERROR",
    }


def test_legacy_gold_evidence_does_not_require_graph_ownership(monkeypatch) -> None:
    monkeypatch.setattr(tool, "settings", Settings(_env_file=None, WORKFLOW_ENGINE="legacy"))
    assert tool._assert_graph_execution_evidence({"found": False}) == []


@pytest.mark.anyio
async def test_gold_checkpoint_cleanup_deletes_only_planned_threads(monkeypatch) -> None:
    saver = SimpleNamespace(adelete_thread=AsyncMock(), alist=None)

    async def alist(_config, *, limit):
        assert limit == 1
        if False:
            yield None

    saver.alist = alist

    class Context:
        async def __aenter__(self):
            return saver

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(gold_replay, "postgres_checkpointer", lambda *_args, **_kwargs: Context())

    assert await gold_replay._delete_gold_graph_checkpoints(["thread-a", "thread-b"]) == 0
    assert saver.adelete_thread.await_args_list[0].args == ("thread-a",)
    assert saver.adelete_thread.await_args_list[1].args == ("thread-b",)


@pytest.mark.anyio
async def test_gold_checkpoint_cleanup_fails_closed_when_thread_remains(monkeypatch) -> None:
    saver = SimpleNamespace(adelete_thread=AsyncMock(), alist=None)

    async def alist(_config, *, limit):
        assert limit == 1
        yield object()

    saver.alist = alist

    class Context:
        async def __aenter__(self):
            return saver

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(gold_replay, "postgres_checkpointer", lambda *_args, **_kwargs: Context())

    with pytest.raises(gold_replay.GoldReplayError) as exc:
        await gold_replay._delete_gold_graph_checkpoints(["thread-a"])
    assert exc.value.code == "GOLD_REPLAY_CHECKPOINT_CLEANUP_VERIFY_FAILED"
