from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import run_live_business_chain_e2e as tool


def _plan(run_id: str = "live-test-001") -> dict:
    return tool.build_plan(run_id, created_at="2026-09-15T00:00:00+00:00")


def test_plan_is_deterministic_except_creation_time_and_hash_bound() -> None:
    first = _plan()
    second = _plan()
    manifest = json.loads(tool.LIVE_MANIFEST_PATH.read_text(encoding="utf-8"))

    assert first == second
    assert first["plan_hash"] == tool.canonical_hash(first)
    assert first["expected_smtp_send_count"] == 1
    assert first["inbound"]["source"] == "existing_imap"
    assert first["inbound"]["message_id"] == manifest["message"]["message_id"]
    assert first["inbound"]["raw_sha256"] == manifest["message"]["raw_sha256"]
    assert first["inbound"]["attachment_count"] == 0
    assert first["egress"]["attachment_ai"] is False
    assert first["egress"]["real_sqlserver"] is False


def test_plan_forbids_temporary_master_data_and_keeps_expected_mapping() -> None:
    plan = _plan()
    gold = plan["master_data_manifest"]["messages"][0]["gold"]

    assert gold["temporary_sn_assets"] == []
    assert gold["temporary_board_cards"] == []
    assert gold["temporary_customer_policies"] == []
    assert plan["cleanup"]["temporary_master_data"] is False
    assert plan["expected"]["items"][0]["material_code"] == "Z.SM.8123V120A"
    assert plan["expected"]["board_items"][0]["board_name"] == "FOVI"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update(expected_smtp_send_count=3), "PLAN_HASH_MISMATCH"),
        (lambda value: value["inbound"].update(to=["someone@example.com"]), "PLAN_HASH_MISMATCH"),
        (lambda value: value["egress"].update(attachment_ai=True), "PLAN_HASH_MISMATCH"),
    ],
)
def test_changed_plan_is_rejected_before_execution(mutation, code: str) -> None:
    plan = _plan()
    original_hash = plan["plan_hash"]
    mutation(plan)

    with pytest.raises(tool.LiveChainError, match=code):
        tool.validate_plan(plan, original_hash, plan["run_id"])


def test_rehashed_plan_with_wrong_send_count_is_still_rejected() -> None:
    plan = _plan()
    plan["expected_smtp_send_count"] = 2
    plan["plan_hash"] = tool.canonical_hash(plan)

    with pytest.raises(tool.LiveChainError, match="SMTP_SEND_COUNT_NOT_EXACTLY_ONE"):
        tool.validate_plan(plan, plan["plan_hash"], plan["run_id"])


def test_run_id_confirmation_is_required() -> None:
    plan = _plan()

    with pytest.raises(tool.LiveChainError, match="RUN_ID_CONFIRMATION_MISMATCH"):
        tool.validate_plan(plan, plan["plan_hash"], "live-test-002")


def test_fixed_imap_plan_uses_dedicated_manifest_without_inbound_smtp() -> None:
    plan = _plan()
    manifest = json.loads(tool.LIVE_MANIFEST_PATH.read_text(encoding="utf-8"))

    assert plan["expected_smtp_send_count"] == 1
    assert "body" not in plan["inbound"]
    assert "plain_text" not in manifest["message"]
    assert plan["master_data_manifest"]["messages"][0]["message_id"] == manifest["message"]["message_id"]
    assert len(plan["expected"]["items"]) == 1
    assert len(plan["expected"]["board_items"]) == 1


def test_pre_relay_validation_stops_on_manifest_mismatch() -> None:
    plan = _plan()
    email = {"email": {"id": 1, "intent_type": "new_repair"}}
    ticket = {
        "ticket": {"id": 2, "current_status_code": "ready_for_export", "missing_fields": {}, **plan["expected"]["fields"]},
        "items": [
            {**item, **next(board for board in plan["expected"]["board_items"] if board["sn"] == item["sn"])}
            for item in plan["expected"]["items"]
        ],
    }
    ticket["items"][0]["material_code"] = "WRONG"

    with pytest.raises(tool.LiveChainError, match="BUSINESS_DATA_VALIDATION_REQUIRES_CONFIRMATION"):
        tool.validate_pre_relay_business_data(email, ticket, plan["expected"])


def test_real_ai_evidence_requires_both_text_calls() -> None:
    rows = [
        {"id": 1, "call_type": "mail_classification", "provider_name": "deepseek", "model_name": "deepseek-chat", "route_name": "mail_classification", "status": "success"},
    ]

    with pytest.raises(tool.LiveChainError, match="REQUIRED_TEXT_AI_CALLS_MISSING"):
        tool.validate_real_ai_calls(rows)


@pytest.mark.parametrize("provider", ["cache:deepseek", "gold-replay", "fake", "mock-provider"])
def test_real_ai_evidence_rejects_substitutes(provider: str) -> None:
    rows = [
        {"id": 1, "call_type": "mail_classification", "provider_name": provider, "model_name": "model", "route_name": "mail_classification", "status": "success"},
        {"id": 2, "call_type": "repair_field_extract", "provider_name": "deepseek", "model_name": "deepseek-chat", "route_name": "repair_field_extract", "status": "success"},
    ]

    with pytest.raises(tool.LiveChainError, match="NON_REAL_TEXT_AI_PROVIDER"):
        tool.validate_real_ai_calls(rows)


def test_real_ai_evidence_accepts_configured_provider() -> None:
    rows = [
        {"id": 1, "call_type": "mail_classification", "provider_name": "deepseek", "model_name": "deepseek-chat", "route_name": "mail_classification", "status": "success", "trace_id": "a"},
        {"id": 2, "call_type": "repair_field_extract", "provider_name": "deepseek", "model_name": "deepseek-chat", "route_name": "repair_field_extract", "status": "success", "trace_id": "b"},
    ]

    evidence = tool.validate_real_ai_calls(rows)

    assert {row["call_type"] for row in evidence} == tool.REQUIRED_TEXT_CALL_TYPES


def test_outbound_mail_requires_exact_thread_and_pdf_hash() -> None:
    message_id = _plan()["inbound"]["message_id"]
    row = {
        "uid": 8,
        "message_id": "<reply@accotest.com>",
        "subject": "[TEST ONLY] RMA授权",
        "from": "rmatest1@accotest.com",
        "to": "rmatest2@accotest.com",
        "cc": "",
        "in_reply_to": message_id,
        "references": message_id,
        "attachments": [{"content_type": "application/pdf", "content_sha256": "a" * 64}],
    }

    result = tool._validate_outbound_mail(row, message_id, "a" * 64)

    assert result["envelope_valid"] is True
    assert result["thread_headers_valid"] is True


def test_outbound_mail_rejects_extra_recipient_or_pdf_mismatch() -> None:
    message_id = _plan()["inbound"]["message_id"]
    row = {
        "subject": "[TEST ONLY] RMA授权",
        "from": "rmatest1@accotest.com",
        "to": "rmatest2@accotest.com, third@example.com",
        "cc": "",
        "in_reply_to": message_id,
        "references": message_id,
        "attachments": [{"content_type": "application/pdf", "content_sha256": "b" * 64}],
    }

    with pytest.raises(tool.LiveChainError, match="OUTBOUND_MAIL_VALIDATION_FAILED"):
        tool._validate_outbound_mail(row, message_id, "a" * 64)


def test_oss_payload_and_pdf_are_hash_verified() -> None:
    payload = b"%PDF-1.7\ncontrolled-test"
    digest = __import__("hashlib").sha256(payload).hexdigest()

    assert tool.validate_oss_payload(
        label="rma_pdf",
        upload_status="success",
        file_size=len(payload),
        stored_sha256=digest,
        payload=payload,
        exists=True,
    ) == digest


@pytest.mark.parametrize(
    "change",
    [
        {"upload_status": "failed"},
        {"file_size": 1},
        {"stored_sha256": "0" * 64},
        {"exists": False},
    ],
)
def test_oss_payload_rejects_upload_failure_missing_object_and_hash_mismatch(change) -> None:
    payload = b"%PDF-1.7\ncontrolled-test"
    values = {
        "label": "rma_pdf",
        "upload_status": "success",
        "file_size": len(payload),
        "stored_sha256": __import__("hashlib").sha256(payload).hexdigest(),
        "payload": payload,
        "exists": True,
    }
    values.update(change)

    with pytest.raises(tool.LiveChainError, match="OSS_CONTENT_VERIFICATION_FAILED"):
        tool.validate_oss_payload(**values)


def test_pdf_part_number_must_equal_relay_call_id() -> None:
    assert tool.validate_pdf_part_no({"items": [{"part_no": "12345"}]}, "12345") == "12345"

    with pytest.raises(tool.LiveChainError, match="PDF_PART_NO_CALL_ID_MAPPING_INVALID"):
        tool.validate_pdf_part_no({"items": [{"part_no": "99999"}]}, "12345")


def test_restore_config_only_restores_send_switches(monkeypatch) -> None:
    captured = {}

    def fake_patch(client, **values):
        captured.update(values)
        return values

    monkeypatch.setattr(tool, "patch_config", fake_patch)
    class Client:
        def data(self, method, path, body=None):
            captured["sn_sync_relay"] = body["relay_sqlserver_enabled"]
            return {}
    initial = {
        "auto_send_enabled": False,
        "auto_followup_enabled": True,
        "rma_auto_send_enabled": False,
        "relay_sqlserver_enabled": True,
        "integrations": {"secret": "must-not-be-written"},
    }

    result = tool._restore_config(Client(), initial)

    assert result == {
        "auto_send_enabled": False,
        "auto_followup_enabled": True,
        "rma_auto_send_enabled": False,
        "relay_sqlserver_enabled": True,
    }
    assert captured["sn_sync_relay"] is True


def test_plan_and_doctor_are_zero_send(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tool, "RESULT_ROOT", tmp_path)
    planned = tool.create_plan("zero-send-001")
    monkeypatch.setattr(tool, "test_mail_configuration_reasons", lambda: [])
    monkeypatch.setattr(tool, "_login", lambda client: 1)
    monkeypatch.setattr(
        tool,
        "current_config",
        lambda client: {
            "integrations": {
                "text_ai_configured": True,
                "text_ai_provider": "deepseek",
                "ai_model": "deepseek-chat",
            }
        },
    )
    monkeypatch.setattr(tool.Client, "request", lambda self, method, path: {"status": "ok"})
    def fake_database_run(coroutine):
        coroutine.close()
        return {"revision_ready": True, "healthy_worker_lease_count": 1, "active_job_ids": []}

    monkeypatch.setattr(tool, "run_database_async", fake_database_run)
    monkeypatch.setattr(tool.settings, "E2E_GOLD_RUN_ENABLED", True)
    monkeypatch.setattr(tool.settings, "RUN_REAL_MAIL_INTEGRATION_TESTS", True)
    monkeypatch.setattr(tool.settings, "AI_API_KEY", "configured")
    monkeypatch.setattr(tool.settings, "AI_MODEL", "model")
    monkeypatch.setattr(tool.settings, "OSS_ENDPOINT", "endpoint")
    monkeypatch.setattr(tool.settings, "OSS_BUCKET", "bucket")
    monkeypatch.setattr(tool.settings, "OSS_ACCESS_KEY", "key")
    monkeypatch.setattr(tool.settings, "OSS_SECRET_KEY", "secret")
    monkeypatch.setattr(tool.settings, "RELAY_ADAPTER", "test_http")
    monkeypatch.setattr(tool.settings, "TEST_RELAY_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setattr(tool.settings, "TEST_RELAY_TOKEN", "token")
    monkeypatch.setenv("RUN_REAL_MAIL_INTEGRATION_TESTS", "1")

    checked = tool.doctor(False)

    assert planned["messages_sent"] == 0
    assert checked["checks"]["messages_sent"] == 0


def test_explicit_run_approvals_are_required() -> None:
    with pytest.raises(tool.LiveChainError, match="EXPLICIT_REAL_MAIL_AND_EGRESS_APPROVAL_REQUIRED"):
        tool.main(
            [
                "run",
                "--plan",
                "unused.json",
                "--plan-hash",
                "a" * 64,
                "--confirm-run-id",
                "live-test-001",
                "--approved-by",
                "tester",
            ]
        )


def test_existing_result_prevents_duplicate_run(monkeypatch, tmp_path: Path) -> None:
    plan = _plan("duplicate-001")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    run_root = tmp_path / plan["run_id"]
    run_root.mkdir()
    (run_root / "result.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tool, "RESULT_ROOT", tmp_path)

    with pytest.raises(tool.LiveChainError, match="RUN_ID_ALREADY_USED"):
        tool.execute_run(plan_path, plan["plan_hash"], plan["run_id"], "tester", 60)


def test_report_contains_no_password_token_or_body_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tool, "RESULT_ROOT", tmp_path)
    root = tmp_path / "report-001"
    root.mkdir()
    safe = {"status": "failed", "error": {"code": "AI_FAILED"}, "cleanup": {"status": "complete"}}
    (root / "result.json").write_text(json.dumps(safe), encoding="utf-8")

    report = tool.report_run("report-001")
    serialized = json.dumps(report).lower()

    assert "password" not in serialized
    assert "token" not in serialized
    assert "body" not in serialized
