from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from email import policy
from email.parser import BytesParser

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
                "max_system_outbound_sends": 1,
                "max_supplement_sends": 0,
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
                            "expected_final_fields": {"customer_code": "CM00001"},
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


def test_manifest_rejects_rma_sent_as_inbound_intent(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["messages"][0]["gold"]["expected_intent"] = "rma_sent"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(tool.GoldCliError) as exc:
        tool.validate_manifest(manifest)

    assert "messages[0].expected_intent_INVALID" in exc.value.details["errors"]


def test_ledger_only_third_uses_fetch_classification_without_email() -> None:
    item = {
        "message_id": "<third@accotest.com>",
        "gold": {
            "expected_intent": "invoice",
            "expected_subtype": None,
            "create_ticket": False,
            "expected_outbound_count": 0,
            "expected_fields": {},
            "expected_items": [],
            "missing_fields": [],
        },
    }
    value = {
        "email_detail": {"email": {}},
        "fetch_result": {
            "intent_type": "invoice",
            "handling_level": "lifecycle_only",
            "fetch_status": "classified_third",
        },
        "ticket_detail": None,
    }

    assert tool._assert_case(item, value, []) == []


def test_manifest_rejects_board_fixture_from_another_case(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["messages"][0]["gold"]["temporary_board_cards"] = [
        {"material_code": "NOT-THIS-CASE", "board_code": "FOVI"}
    ]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(tool.GoldCliError) as exc:
        tool.validate_manifest(manifest)

    assert any(
        value.endswith("material_code_NOT_IN_EXPECTED_ITEMS")
        for value in exc.value.details["errors"]
    )


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
    first = RelayRecord(request_id="55555555-5555-4555-8555-555555555555", ticket_id=9, ticket_item_id=1, sn="SN-1")
    second = RelayRecord(request_id="66666666-6666-4666-8666-666666666666", ticket_id=9, ticket_item_id=2, sn="SN-2")
    first_result = store.create(first)
    store.create(second)
    rows = store.query([first.request_id, second.request_id])
    assert {row["rma_no"] for row in rows} == {"2026081201"}
    assert store.create(first)["remote_record_key"] == first_result["remote_record_key"]
    assert store.create(first)["idempotent_reuse"] is True


def test_cleanup_apply_requires_preview_plan_hash(tmp_path: Path) -> None:
    parser = tool.build_parser()
    args = parser.parse_args(["cleanup", "--manifest", str(_manifest(tmp_path)), "--apply"])
    assert args.apply is True
    assert args.plan_hash is None


def test_cleanup_reports_partial_success_when_relay_reset_fails(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    root = tmp_path / payload["suite_id"]
    root.mkdir()
    responses = iter(
        [
            {"plan_hash": "mail-plan", "blockers": []},
            {"blockers": []},
            {"status": "applied"},
        ]
    )

    def run_async(coroutine):
        coroutine.close()
        return next(responses)

    monkeypatch.setattr(tool, "suite_root", lambda _suite_id: root)
    monkeypatch.setattr(tool, "run_database_async", run_async)
    monkeypatch.setattr(
        tool,
        "_relay_reset",
        lambda: (_ for _ in ()).throw(PermissionError("secret relay response")),
    )
    combined = {
        "mail_plan_hash": "mail-plan",
        "temporary_master_plan": {"blockers": []},
    }
    plan_hash = hashlib.sha256(
        json.dumps(
            combined, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(tool.GoldCliError) as exc:
        tool.cleanup(manifest, apply=True, expected_hash=plan_hash)

    assert exc.value.code == "CLEANUP_RELAY_RESET_FAILED"
    assert exc.value.details == {
        "database_cleanup_applied": True,
        "temporary_master_cleanup_count": 0,
        "relay_reset": False,
        "cause": "PermissionError",
    }


def test_stale_classification_master_state_is_cleaned_before_reuse(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "case-manifest.json"
    state = tmp_path / "temporary-master-state.json"
    manifest.write_text("{}", encoding="utf-8")
    state.write_text("{}", encoding="utf-8")
    calls = []

    async def cleanup(manifest_path, *, state_path, skip_manifest_validation):
        calls.append((manifest_path, state_path, skip_manifest_validation))
        return {"status": "cleaned"}

    monkeypatch.setattr(tool, "cleanup_temporary_master_data", cleanup)

    result = asyncio.run(tool._cleanup_stale_temporary_master_state(manifest, state))

    assert result == {"status": "cleaned"}
    assert calls == [(manifest, state, True)]


def test_missing_classification_master_state_needs_no_cleanup(tmp_path: Path) -> None:
    result = asyncio.run(
        tool._cleanup_stale_temporary_master_state(
            tmp_path / "case-manifest.json",
            tmp_path / "missing-state.json",
        )
    )

    assert result is None


def test_post_case_reset_waits_only_for_transient_active_jobs(monkeypatch) -> None:
    responses = iter(
        [
            {
                "plan_hash": "busy",
                "blockers": ["GOLD_REPLAY_ACTIVE_JOBS"],
                "active_job_ids": [91],
            },
            {"plan_hash": "ready", "blockers": [], "active_job_ids": []},
            {"plan_hash": "ready", "blockers": [], "active_job_ids": []},
            {"status": "applied"},
        ]
    )

    def run_async(coroutine):
        coroutine.close()
        return next(responses)

    monkeypatch.setattr(tool, "run_database_async", run_async)
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)

    result = tool._reset_after_active_jobs_idle(
        ["<gold@example.com>"],
        suite_id="suite",
        run_id="post",
        timeout_seconds=5,
    )

    assert result == {"status": "applied"}


def test_post_case_reset_retries_apply_race_until_downstream_job_is_visible(
    monkeypatch,
) -> None:
    responses = iter(
        [
            {"plan_hash": "premature", "blockers": [], "active_job_ids": []},
            {"plan_hash": "premature", "blockers": [], "active_job_ids": []},
            tool.GoldCliError("CLEANUP_PLAN_HASH_MISMATCH"),
            {
                "plan_hash": "busy",
                "blockers": ["GOLD_REPLAY_ACTIVE_JOBS"],
                "active_job_ids": [92],
            },
            {"plan_hash": "ready", "blockers": [], "active_job_ids": []},
            {"plan_hash": "ready", "blockers": [], "active_job_ids": []},
            {"status": "applied"},
        ]
    )

    def run_async(coroutine):
        coroutine.close()
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(tool, "run_database_async", run_async)
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)

    result = tool._reset_after_active_jobs_idle(
        ["<gold@example.com>"],
        suite_id="suite",
        run_id="post",
        timeout_seconds=5,
    )

    assert result == {"status": "applied"}


def test_post_case_reset_does_not_wait_for_uncertain_external_state(
    monkeypatch,
) -> None:
    def run_async(coroutine):
        coroutine.close()
        return {
            "plan_hash": "blocked",
            "blockers": ["GOLD_REPLAY_UNCERTAIN_EXTERNAL_OPERATIONS"],
        }

    monkeypatch.setattr(tool, "run_database_async", run_async)

    with pytest.raises(tool.GoldCliError) as exc:
        tool._reset_after_active_jobs_idle(
            ["<gold@example.com>"],
            suite_id="suite",
            run_id="post",
        )

    assert exc.value.code == "CLEANUP_BLOCKED"


def test_post_case_reset_waits_while_active_job_owns_uncertain_operation(
    monkeypatch,
) -> None:
    responses = iter(
        [
            {
                "plan_hash": "busy",
                "blockers": [
                    "GOLD_REPLAY_ACTIVE_JOBS",
                    "GOLD_REPLAY_UNCERTAIN_EXTERNAL_OPERATION",
                ],
                "active_job_ids": [93],
            },
            {"plan_hash": "ready", "blockers": [], "active_job_ids": []},
            {"plan_hash": "ready", "blockers": [], "active_job_ids": []},
            {"status": "applied"},
        ]
    )

    def run_async(coroutine):
        coroutine.close()
        return next(responses)

    monkeypatch.setattr(tool, "run_database_async", run_async)
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)

    result = tool._reset_after_active_jobs_idle(
        ["<gold@example.com>"],
        suite_id="suite",
        run_id="post",
        timeout_seconds=5,
    )

    assert result == {"status": "applied"}


def test_run_parser_accepts_controlled_message_id_subset(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    args = tool.build_parser().parse_args(
        [
            "run",
            "--manifest",
            str(manifest),
            "--confirm-suite",
            "suite-001",
            "--message-id",
            "<gold-001@accotest.com>",
        ]
    )

    assert args.message_id == ["<gold-001@accotest.com>"]


def test_real_mail_suite_lock_rejects_concurrent_runner(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tool, "EVIDENCE_ROOT", tmp_path)

    with tool._exclusive_suite_run("suite-lock", "first"):
        with pytest.raises(tool.GoldCliError) as exc:
            with tool._exclusive_suite_run("suite-lock", "second"):
                pytest.fail("concurrent runner must not enter the lock")

    assert exc.value.code == "REAL_MAIL_RUN_ALREADY_ACTIVE"


def test_set_and_verify_runtime_switches_detects_drift(monkeypatch) -> None:
    monkeypatch.setattr(tool, "patch_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tool,
        "current_config",
        lambda _client: {
            "auto_send_enabled": False,
            "auto_followup_enabled": False,
            "rma_auto_send_enabled": False,
        },
    )

    with pytest.raises(tool.GoldCliError) as exc:
        tool._set_and_verify_config(
            object(),
            auto_send_enabled=True,
            auto_followup_enabled=False,
            rma_auto_send_enabled=False,
        )

    assert exc.value.code == "RUNTIME_SEND_SWITCH_DRIFT"


def test_target_relay_gate_rejects_sqlserver_worker() -> None:
    with pytest.raises(tool.GoldCliError) as exc:
        tool._assert_target_relay_is_test_http(
            {
                "integrations": {
                    "sqlserver_relay": {
                        "adapter": "sqlserver",
                        "configured": True,
                        "status": "configured",
                        "missing": [],
                    }
                }
            }
        )

    assert exc.value.code == "TARGET_RELAY_NOT_TEST_HTTP"


def test_target_relay_gate_accepts_configured_test_http_worker() -> None:
    tool._assert_target_relay_is_test_http(
        {
            "integrations": {
                "sqlserver_relay": {
                    "adapter": "test_http",
                    "configured": True,
                    "status": "configured",
                    "missing": [],
                }
            }
        }
    )


def test_config_restore_does_not_mask_primary_error(monkeypatch) -> None:
    monkeypatch.setattr(
        tool,
        "patch_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("MAIL_TEST_PREFLIGHT_FAILED:sensitive")
        ),
    )
    primary = ValueError("original failure")

    tool._restore_config_without_masking_primary_error(object(), {}, primary)

    assert primary.__notes__ == [
        "RUNTIME_CONFIG_RESTORE_FAILED:MAIL_TEST_PREFLIGHT_FAILED"
    ]


def test_config_restore_failure_is_structured_without_primary(monkeypatch) -> None:
    monkeypatch.setattr(
        tool,
        "patch_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("secret")),
    )

    with pytest.raises(tool.GoldCliError) as exc:
        tool._restore_config_without_masking_primary_error(object(), {}, None)

    assert exc.value.code == "RUNTIME_CONFIG_RESTORE_FAILED"
    assert exc.value.details == {"cause": "PermissionError"}


def test_config_restore_includes_relay_runtime_switch(monkeypatch) -> None:
    captured = {}

    def patch(_client, **values):
        captured.update(values)

    monkeypatch.setattr(tool, "patch_config", patch)

    tool._restore_config_without_masking_primary_error(
        object(),
        {
            "auto_send_enabled": False,
            "auto_followup_enabled": True,
            "rma_auto_send_enabled": False,
            "relay_sqlserver_enabled": True,
        },
        None,
    )

    assert captured["relay_sqlserver_enabled"] is True


def test_safe_exception_code_preserves_only_machine_prefix() -> None:
    assert (
        tool._safe_exception_code(
            RuntimeError("TEMPORARY_BOARD_CARD_ALREADY_EXISTS:sensitive-value")
        )
        == "TEMPORARY_BOARD_CARD_ALREADY_EXISTS"
    )
    assert tool._safe_exception_code(RuntimeError("free-form sensitive text")) == "RuntimeError"


def test_classification_source_hash_changes_with_business_source(tmp_path: Path) -> None:
    source_root = tmp_path / "app"
    source_root.mkdir()
    source = source_root / "service.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    first = tool._classification_source_sha256(source_root)

    source.write_text("VALUE = 2\n", encoding="utf-8")

    assert tool._classification_source_sha256(source_root) != first


def test_classification_source_hash_changes_with_llm_routes(tmp_path: Path) -> None:
    source_root = tmp_path / "app"
    source_root.mkdir()
    (source_root / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    config_root = tmp_path / "config"
    config_root.mkdir()
    routes = config_root / "llm_routes.yaml"
    routes.write_text("tasks: {mail_classification: {model: one}}\n", encoding="utf-8")
    first = tool._classification_source_sha256(source_root)

    routes.write_text("tasks: {mail_classification: {model: two}}\n", encoding="utf-8")

    assert tool._classification_source_sha256(source_root) != first


def test_classification_gate_rejects_changed_business_source(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(tool, "EVIDENCE_ROOT", evidence_root)
    suite = evidence_root / payload["suite_id"]
    suite.mkdir(parents=True)
    evidence = suite / "classification-baseline.json"
    evidence.write_text("{}", encoding="utf-8")
    (suite / "classification-gate.json").write_text(
        json.dumps(
            {
                "manifest_sha256": tool.file_sha256(manifest),
                "classification_source_sha256": "old-source",
                "classification_evidence_sha256": tool.file_sha256(evidence),
                "status": "passed",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "_classification_source_sha256", lambda: "new-source")

    with pytest.raises(tool.GoldCliError) as exc:
        tool._require_classification_gate(manifest, payload)

    assert exc.value.code == "CLASSIFICATION_SOURCE_CHANGED"


def test_classification_issues_rejects_wrong_business_stage(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    message_id = manifest["messages"][0]["message_id"]
    result = {
        "cases": [
                {
                    "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
                    "intent_type": "new_repair",
                    "handling_level": "auto_repair",
                    "persistence_tier": "business",
                "ticket": {
                    "status": "manual_review",
                    "customer_code": "CM00001",
                    "missing_fields": {},
                },
                "items": [{"sn": "SN-GOLD-001"}],
            }
        ]
    }

    issues = tool._classification_issues(manifest, result)

    assert issues == [
        {
            "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
            "codes": ["CLASSIFICATION_STAGE_MISMATCH"],
        }
    ]


def test_classification_accepts_resolved_problem_description(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gold = manifest["messages"][0]["gold"]
    gold["missing_fields"] = ["problem_description"]
    message_id = manifest["messages"][0]["message_id"]
    result = {
        "cases": [{
            "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
            "intent_type": "new_repair",
            "handling_level": "auto_repair",
            "persistence_tier": "business",
            "ticket": {
                "status": "ready_for_export",
                "customer_code": "CM00001",
                "problem_description": "Explicit failure",
                "missing_fields": {},
            },
            "items": [{"sn": "SN-GOLD-001"}],
        }]
    }

    assert tool._classification_issues(manifest, result) == []


def test_classification_accepts_explicit_manual_review_outcome(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gold = manifest["messages"][0]["gold"]
    gold["missing_fields"] = ["contact_person", "contact_phone", "mailing_address"]
    gold["expected_final_status"] = "manual_review"
    gold["allow_manual_review"] = True
    message_id = manifest["messages"][0]["message_id"]
    result = {
        "cases": [{
            "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
            "intent_type": "new_repair",
            "handling_level": "auto_repair",
            "persistence_tier": "business",
            "ticket": {
                "status": "manual_review",
                "customer_code": "CM00001",
                "missing_fields": {
                    "contact_person": "required",
                    "contact_phone": "required",
                    "mailing_address": "required",
                },
            },
            "items": [{"sn": "SN-GOLD-001"}],
        }]
    }

    assert tool._classification_issues(manifest, result) == []


def test_manual_review_then_rma_uses_operator_workflow() -> None:
    calls: list[tuple[str, str, dict | None]] = []

    class Client:
        def data(self, method: str, path: str, body: dict | None = None, **_kwargs):
            calls.append((method, path, body))
            if path.endswith("/fields"):
                return {"ticket": {"id": 7}}
            if path.endswith("/validate-sn"):
                return {"ticket": {"id": 7, "sn_validation_status": "passed"}}
            if method == "GET":
                return {
                    "ticket": {"id": 7, "current_status_code": "manual_review"},
                    "manual_tasks": [
                        {"id": 19, "task_type": "ai_review_required", "status": "pending"}
                    ],
                }
            return {
                "safety_result": {"status": "ready_for_export"},
                "ticket": {"id": 7, "current_status_code": "ready_for_export"},
            }

    result = tool._resolve_manual_review_for_rma(
        Client(),
        ticket_detail={"ticket": {"id": 7, "current_status_code": "manual_review"}},
        gold={"manual_resolution_fields": {"request_date": "2026-08-12"}},
        suite_id="gold-suite",
    )

    assert result["safety_result"]["status"] == "ready_for_export"
    assert [call[:2] for call in calls] == [
        ("PATCH", "/api/v1/tickets/7/fields"),
        ("POST", "/api/v1/tickets/7/validate-sn"),
        ("GET", "/api/v1/tickets/7"),
        ("POST", "/api/v1/manual-review/tasks/19/resolve"),
    ]
    assert calls[-1][2]["next_action"] == "transition_ready_for_export"


def test_known_repair_need_customer_info_uses_manual_fields_then_export_gate() -> None:
    calls: list[tuple[str, str]] = []

    class Client:
        def data(self, method: str, path: str, **_kwargs):
            calls.append((method, path))
            if path.endswith("/validate-sn"):
                return {"ticket": {"id": 7, "sn_validation_status": "passed"}}
            if path.endswith("/validate-export"):
                return {"status": "ready_for_export", "ticket_id": 7}
            return {"ticket": {"id": 7, "current_status_code": "need_customer_info"}}

    result = tool._resolve_manual_review_for_rma(
        Client(),
        ticket_detail={"ticket": {"id": 7, "current_status_code": "need_customer_info"}},
        gold={"manual_resolution_fields": {"request_date": "2026-08-12"}},
        suite_id="gold-suite",
    )

    assert result["status"] == "ready_for_export"
    assert ("POST", "/api/v1/tickets/7/validate-export") in calls


def test_known_repair_does_not_repeat_export_validation_after_sn_validation_advanced_ticket() -> None:
    calls: list[tuple[str, str]] = []

    class Client:
        def data(self, method: str, path: str, **_kwargs):
            calls.append((method, path))
            if path.endswith("/validate-sn"):
                return {"ticket": {"id": 7, "sn_validation_status": "passed"}}
            if method == "GET":
                return {"ticket": {"id": 7, "current_status_code": "ready_for_export"}}
            return {"ticket": {"id": 7, "current_status_code": "need_customer_info"}}

    result = tool._resolve_manual_review_for_rma(
        Client(),
        ticket_detail={"ticket": {"id": 7, "current_status_code": "need_customer_info"}},
        gold={"manual_resolution_fields": {"request_date": "2026-08-12"}},
        suite_id="gold-suite",
    )

    assert result["ticket"]["current_status_code"] == "ready_for_export"
    assert not any(path.endswith("/validate-export") for _, path in calls)


def test_known_repair_accepts_only_verified_ready_state_after_export_transition_race() -> None:
    get_count = 0

    class Client:
        def data(self, method: str, path: str, **_kwargs):
            nonlocal get_count
            if path.endswith("/validate-sn"):
                return {"ticket": {"id": 7, "sn_validation_status": "passed"}}
            if method == "GET":
                get_count += 1
                status = "need_customer_info" if get_count == 1 else "ready_for_export"
                return {"ticket": {"id": 7, "current_status_code": status}}
            if path.endswith("/validate-export"):
                raise RuntimeError("WORKFLOW_TRANSITION_NOT_ALLOWED")
            return {"ticket": {"id": 7}}

    result = tool._resolve_manual_review_for_rma(
        Client(),
        ticket_detail={"ticket": {"id": 7, "current_status_code": "need_customer_info"}},
        gold={"manual_resolution_fields": {"request_date": "2026-08-12"}},
        suite_id="gold-suite",
    )

    assert result["ticket"]["current_status_code"] == "ready_for_export"
    assert get_count == 2


def test_classification_accepts_email_level_manual_review_for_known_repair(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gold = manifest["messages"][0]["gold"]
    gold["allow_manual_review"] = True
    gold["send_mode"] = "manual_review_then_rma"
    message_id = manifest["messages"][0]["message_id"]
    result = {
        "cases": [{
            "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
            "email_id": 17,
            "intent_type": "unknown",
            "handling_level": "unknown",
            "persistence_tier": "minimal",
            "classification_reason_code": "PRECLASSIFICATION_PROVIDER_FAILED",
            "parse_status": "manual_review",
            "ticket": None,
            "items": [],
        }]
    }

    assert tool._classification_issues(manifest, result) == []


def test_email_level_manual_review_promotes_known_repair() -> None:
    calls: list[tuple[str, str, dict | None, dict | None]] = []

    class Client:
        def data(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
            calls.append((method, path, body, params))
            if method == "GET":
                return {"items": [{
                    "id": 23,
                    "email_id": 17,
                    "ticket_id": None,
                    "task_type": "unknown_mail_classification",
                    "status": "pending",
                }]}
            return {"reparse_result": {"status": "promoted", "email_id": 17}}

    result = tool._promote_email_to_known_repair(Client(), email_id=17, suite_id="gold-suite")

    assert result["reparse_result"]["status"] == "promoted"
    assert calls[-1][1] == "/api/v1/manual-review/tasks/23/resolve"
    assert calls[-1][2]["next_action"] == "promote_to_first"
    assert calls[-1][2]["target_first_intent"] == "new_repair"


def test_email_level_manual_review_ignores_unrelated_tasks() -> None:
    class Client:
        def data(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
            if method == "GET":
                return {"items": [
                    {"id": 21, "email_id": 17, "ticket_id": None, "task_type": "manual_rma_business", "status": "pending"},
                    {"id": 23, "email_id": 17, "ticket_id": None, "task_type": "ai_unavailable", "status": "assignment_failed"},
                ]}
            return {"task_id": 23}

    assert tool._promote_email_to_known_repair(Client(), email_id=17, suite_id="gold-suite") == {"task_id": 23}


def test_wait_for_parse_terminal_does_not_return_while_ticket_parse_is_pending(monkeypatch) -> None:
    details = [
        {"email": {"parse_status": "pending"}, "parse_results": [{"ticket_id": 7}]},
        {"email": {"parse_status": "needs_manual"}, "parse_results": [{"ticket_id": 7}]},
    ]

    class Client:
        def data(self, method: str, path: str, **_kwargs):
            if path == "/api/v1/emails/17":
                return details.pop(0)
            return {"ticket": {"id": 7, "current_status_code": "manual_review"}}

    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)
    email_detail, ticket_detail = tool._wait_for_parse_terminal(Client(), 17, timeout_seconds=1)

    assert email_detail["email"]["parse_status"] == "needs_manual"
    assert ticket_detail["ticket"]["current_status_code"] == "manual_review"


def test_manual_review_accepts_nested_ticket_detail_response() -> None:
    calls: list[tuple[str, str]] = []

    class Client:
        def data(self, method: str, path: str, **kwargs):
            calls.append((method, path))
            if path.endswith("/fields"):
                return {"ticket": {"id": 7}}
            if path.endswith("/validate-sn"):
                return {"ticket": {"sn_validation_status": "passed"}}
            if path == "/api/v1/tickets/7":
                return {
                    "ticket": {"id": 7, "current_status_code": "manual_review"},
                    "manual_tasks": [{"id": 9, "task_type": "ai_review_required", "status": "pending"}],
                }
            if path.endswith("/resolve"):
                return {"ticket": {"ticket": {"id": 7, "current_status_code": "ready_for_export"}}}
            return {}

    result = tool._resolve_manual_review_for_rma(
        Client(),
        ticket_detail={
            "ticket": {"id": 7, "current_status_code": "manual_review"},
            "manual_tasks": [{"id": 9, "task_type": "ai_review_required", "status": "pending"}],
        },
        gold={
            "manual_resolution_fields": {"contact_person": "张跃"},
        },
        suite_id="gold-suite",
    )

    assert result["ticket"]["ticket"]["current_status_code"] == "ready_for_export"


def test_refresh_authoritative_send_counts_uses_both_mailboxes(monkeypatch) -> None:
    monkeypatch.setattr(
        tool,
        "_suite_mailbox_counts",
        lambda **_kwargs: ([{"uid": 11}, {"uid": 12}], [{"uid": 21}]),
    )
    result: dict = {}

    system, supplements = tool._refresh_authoritative_send_counts(
        result,
        rmatest2_baseline_uid=10,
        rmatest1_baseline_uid=20,
        original_message_ids=["<original@accotest.com>"],
        supplement_message_ids={"<supplement@accotest.com>"},
    )

    assert len(system) == 2
    assert len(supplements) == 1
    assert result["actual_system_outbound_count"] == 2
    assert result["actual_supplement_send_count"] == 1
    assert result["actual_total_smtp_count"] == 3


def test_mailbox_evidence_saves_only_selected_thread(monkeypatch, tmp_path: Path) -> None:
    def raw_message(message_id: str, in_reply_to: str, payload: bytes) -> bytes:
        return (
            "From: rmatest1@accotest.com\r\n"
            "To: rmatest2@accotest.com\r\n"
            f"Message-ID: {message_id}\r\n"
            f"In-Reply-To: {in_reply_to}\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=x\r\n\r\n"
            "--x\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            "--x\r\nContent-Type: application/pdf\r\n"
            "Content-Disposition: attachment; filename=rma.pdf\r\n"
            "Content-Transfer-Encoding: base64\r\n\r\n"
        ).encode() + __import__("base64").b64encode(payload) + b"\r\n--x--\r\n"

    selected = raw_message("<selected-reply@test>", "<selected@test>", b"pdf-selected")
    unrelated = raw_message("<unrelated-reply@test>", "<unrelated@test>", b"pdf-unrelated")

    class Imap:
        def select(self, *_args, **_kwargs):
            return "OK", [b""]

        def uid(self, command, uid, *_args):
            if command == "search":
                return "OK", [b"11 12"]
            return "OK", [(b"", selected if uid == b"11" else unrelated)]

        def logout(self):
            return None

    monkeypatch.setattr(tool, "_imap_connect", lambda **_kwargs: Imap())
    evidence_dir = tmp_path / "evidence"

    rows = tool._mailbox_new_messages(
        10,
        host="imap.test",
        port=993,
        user="user",
        password="password",
        folder="INBOX",
        use_ssl=True,
        evidence_dir=evidence_dir,
        evidence_thread_message_ids={"<selected@test>"},
    )

    assert len(rows) == 2
    assert sorted(path.name for path in evidence_dir.iterdir()) == [
        "uid-11-attachment-1.pdf",
        "uid-11.eml",
    ]


def test_mailbox_read_ignores_abort_during_logout(monkeypatch) -> None:
    class Imap:
        def select(self, *_args, **_kwargs):
            return "OK", [b""]

        def uid(self, command, *_args):
            if command == "search":
                return "OK", [b""]
            raise AssertionError("fetch should not run")

        def logout(self):
            raise __import__("imaplib").IMAP4.abort("server closed")

    monkeypatch.setattr(tool, "_imap_connect", lambda **_kwargs: Imap())

    assert tool._mailbox_new_messages(
        10,
        host="imap.test",
        port=993,
        user="user",
        password="password",
        folder="INBOX",
        use_ssl=True,
    ) == []


def test_mailbox_read_reconnects_after_transient_abort(monkeypatch) -> None:
    calls = {"count": 0}

    class Imap:
        def __init__(self, fail: bool):
            self.fail = fail

        def select(self, *_args, **_kwargs):
            if self.fail:
                raise __import__("imaplib").IMAP4.abort("transient close")
            return "OK", [b""]

        def uid(self, command, *_args):
            assert command == "search"
            return "OK", [b""]

        def logout(self):
            return None

    def connect(**_kwargs):
        calls["count"] += 1
        return Imap(fail=calls["count"] == 1)

    monkeypatch.setattr(tool, "_imap_connect", connect)
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)

    assert tool._mailbox_new_messages(
        10,
        host="imap.test",
        port=993,
        user="user",
        password="password",
        folder="INBOX",
        use_ssl=True,
    ) == []
    assert calls["count"] == 2


def test_mailbox_max_uid_reconnects_after_transient_abort(monkeypatch) -> None:
    calls = {"count": 0}

    class Imap:
        def __init__(self, fail: bool):
            self.fail = fail

        def select(self, *_args, **_kwargs):
            if self.fail:
                raise __import__("imaplib").IMAP4.abort("transient close")
            return "OK", [b""]

        def uid(self, command, *_args):
            assert command == "search"
            return "OK", [b"8 11 19"]

        def logout(self):
            return None

    def connect(**_kwargs):
        calls["count"] += 1
        return Imap(fail=calls["count"] == 1)

    monkeypatch.setattr(tool, "_imap_connect", connect)
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)

    assert tool._mailbox_max_uid(
        host="imap.test",
        port=993,
        user="user",
        password="password",
        folder="INBOX",
        use_ssl=True,
    ) == 19
    assert calls["count"] == 2


def test_sensitive_egress_approval_is_explicit_and_hash_bound(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["suite_id"] = f"suite-egress-{hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]}"
    payload["messages"][0]["attachments"] = []
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(tool.GoldCliError) as exc:
        tool._require_sensitive_egress_approval(manifest, payload)
    assert exc.value.code == "SENSITIVE_EGRESS_APPROVAL_REQUIRED"

    approved = tool.authorize_sensitive_egress(
        manifest, approved_by="business-owner", acknowledge=True
    )
    assert approved["destinations"] == ["project_oss", "deepseek_api"]
    assert tool._require_sensitive_egress_approval(manifest, payload)[
        "manifest_sha256"
    ] == tool.file_sha256(manifest)

    payload["messages"][0]["gold"]["expected_fields"]["customer_code"] = "CHANGED"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(tool.GoldCliError) as exc:
        tool._require_sensitive_egress_approval(manifest, payload)
    assert exc.value.code == "SENSITIVE_EGRESS_APPROVAL_MANIFEST_CHANGED"


def test_manifest_counts_customer_supplements_in_total_smtp_limit(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    gold = payload["messages"][0]["gold"]
    gold["send_mode"] = "followup_then_rma"
    gold["expected_outbound_count"] = 2
    gold["supplement"] = {"body_text": "联系电话：13800000000"}
    payload["max_system_outbound_sends"] = 2
    payload["max_supplement_sends"] = 1
    payload["max_actual_sends"] = 3
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = tool.validate_manifest(manifest)
    assert result["planned_system_outbound_sends"] == 2
    assert result["planned_supplement_sends"] == 1
    assert result["planned_total_smtp_sends"] == 3

    payload["max_actual_sends"] = 2
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(tool.GoldCliError) as exc:
        tool.validate_manifest(manifest)
    assert "MAX_ACTUAL_SENDS_MUST_EQUAL_ALL_PLANNED_SMTP_SENDS" in exc.value.details[
        "errors"
    ]


def test_supplement_envelope_is_test_only_and_threaded(monkeypatch) -> None:
    captured: dict[str, bytes] = {}

    class Smtp:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def login(self, *_args):
            return None

        def send_message(self, msg, *, from_addr, to_addrs):
            captured["raw"] = msg.as_bytes()
            assert from_addr == "rmatest2@accotest.com"
            assert to_addrs == ["rmatest1@accotest.com"]

    monkeypatch.setattr(tool.smtplib, "SMTP_SSL", Smtp)
    message_id = tool._send_supplement(
        "<original@accotest.com>",
        {
            "message_id": "<followup@accotest.com>",
            "subject": "[TEST ONLY] Re: repair",
            "from": "rmatest1@accotest.com",
            "date": "Thu, 13 Aug 2026 12:00:00 +0800",
            "_plain_body": "请补充联系电话。",
            "_html_body": "<div>请补充联系电话。</div>",
        },
        {"subject": "Re: repair", "body_text": "联系电话：13800000000"},
        sent_so_far=0,
        hard_limit=1,
    )
    parsed = BytesParser(policy=policy.default).parsebytes(captured["raw"])
    assert message_id.startswith("<")
    assert parsed["Subject"].startswith("[TEST ONLY]")
    assert parsed["To"] == "rmatest1@accotest.com"
    assert parsed["In-Reply-To"] == "<followup@accotest.com>"
    assert "<original@accotest.com>" in parsed["References"]
    assert "> 请补充联系电话。" in parsed.get_body(preferencelist=("plain",)).get_content()
    assert "blockquote" in parsed.get_body(preferencelist=("html",)).get_content()


def test_supplement_send_checks_limit_before_smtp(monkeypatch) -> None:
    monkeypatch.setattr(
        tool.smtplib,
        "SMTP_SSL",
        lambda *_args, **_kwargs: pytest.fail("SMTP must not be opened"),
    )
    with pytest.raises(tool.GoldCliError) as exc:
        tool._send_supplement(
            "<original@accotest.com>",
            {"message_id": "<followup@accotest.com>"},
            {"body_text": "联系电话：13800000000"},
            sent_so_far=1,
            hard_limit=1,
        )
    assert exc.value.code == "SUPPLEMENT_SEND_HARD_LIMIT_EXCEEDED"


def test_resume_pending_followup_reenters_idempotent_draft_endpoint() -> None:
    calls: list[tuple[str, str, dict]] = []

    class Client:
        def data(self, method, path, **kwargs):
            calls.append((method, path, kwargs.get("body") or {}))
            return {}

    tool._resume_pending_reply_after_enabling(
        Client(),
        email_id=44,
        ticket_detail={
            "ticket": {
                "id": 55,
                "current_status_code": "need_customer_info",
                "language_code": "zh-CN",
                "missing_fields": {"contact_phone": "required"},
            },
            "reply_records": [
                {
                    "reply_type": "missing_fields",
                    "send_status": "pending_review",
                }
            ],
        },
    )

    assert calls == [
        (
            "POST",
            "/api/v1/replies/55/draft",
            {
                "reply_type": "missing_fields",
                "related_email_id": 44,
                "language": "zh-CN",
                "missing_fields": {"contact_phone": "required"},
            },
        )
    ]


def test_fetch_system_message_retries_until_smtp_mail_is_imap_visible(monkeypatch) -> None:
    calls = 0

    class Client:
        def data(self, method, path, **kwargs):
            nonlocal calls
            calls += 1
            return {"job": {"id": calls}}

    jobs = [
        {"result_json": {"fetched": []}},
        {
            "result_json": {
                "fetched": [
                    {"message_id": "<supplement@accotest.com>", "email_id": 88}
                ]
            }
        },
    ]
    monkeypatch.setattr(tool, "wait_for_job", lambda *_args, **_kwargs: jobs.pop(0))
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)

    email_id, row = tool._fetch_system_message(
        Client(), "<supplement@accotest.com>", availability_timeout_seconds=1
    )

    assert email_id == 88
    assert row["message_id"] == "<supplement@accotest.com>"
    assert calls == 2


def test_fetch_system_message_waits_for_async_ingress_job(monkeypatch) -> None:
    class Client:
        def data(self, method, path, **kwargs):
            return {"job": {"id": 71}}

    jobs = [
        {
            "result_json": {
                "fetched": [
                    {
                        "message_id": "<async@accotest.com>",
                        "email_id": None,
                        "fetch_status": "spooled",
                        "processing_job_id": 72,
                    }
                ]
            }
        },
        {"result_json": {"status": "success", "email_id": 93}},
    ]
    waited: list[int] = []

    def wait(_client, job_id):
        waited.append(job_id)
        return jobs.pop(0)

    monkeypatch.setattr(tool, "wait_for_job", wait)
    monkeypatch.setattr(
        tool,
        "find_email",
        lambda *_args: pytest.fail("ingress job result should provide email_id"),
    )

    email_id, row = tool._fetch_system_message(Client(), "<async@accotest.com>")

    assert email_id == 93
    assert row["fetch_status"] == "spooled"
    assert waited == [71, 72]


def test_fetch_system_message_retries_reused_fetch_job(monkeypatch) -> None:
    responses = iter(
        [
            {"job": {"id": 80}, "reused": True},
            {"job": {"id": 81}, "reused": False},
        ]
    )

    class Client:
        def data(self, method, path, **kwargs):
            return next(responses)

    waited: list[int] = []

    def wait(_client, job_id):
        waited.append(job_id)
        return {
            "result_json": {
                "fetched": [
                    {"message_id": "<fresh@accotest.com>", "email_id": 94}
                ]
            }
        }

    monkeypatch.setattr(tool, "wait_for_job", wait)
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)

    email_id, _ = tool._fetch_system_message(Client(), "<fresh@accotest.com>")

    assert email_id == 94
    assert waited == [81]


def test_wait_for_case_approves_special_policy_then_manual_reply(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    ticket_details = [
        {
            "ticket": {"id": 55, "current_status_code": "ready_for_export"},
            "manual_tasks": [{"task_type": "rma_special_policy_review", "status": "pending"}],
            "reply_records": [],
        },
        {
            "ticket": {"id": 55, "current_status_code": "ready_for_export"},
            "manual_tasks": [],
            "reply_records": [{"id": 77, "reply_type": "rma_authorization", "send_status": "pending_review"}],
        },
        {
            "ticket": {"id": 55, "current_status_code": "rma_sent"},
            "manual_tasks": [],
            "reply_records": [{"id": 77, "reply_type": "rma_authorization", "send_status": "sent"}],
        },
    ]

    class Client:
        def data(self, method, path, **kwargs):
            calls.append((method, path, kwargs.get("body") or {}))
            if path == "/api/v1/emails/44":
                return {"parse_results": [{"ticket_id": 55}]}
            if path == "/api/v1/tickets/55":
                return ticket_details.pop(0)
            return {}

    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)
    value = tool._wait_for_case(
        Client(), 44, "rma_sent", 1, timeout_seconds=1, approve_special_policy=True
    )

    assert value["ticket_detail"]["ticket"]["current_status_code"] == "rma_sent"
    assert any(path.endswith("/rma/manual-policy-approve") for _, path, _ in calls)
    assert any(path == "/api/v1/replies/77/approve-send/jobs" for _, path, _ in calls)


def test_closed_satisfies_legacy_rma_sent_gold_expectation() -> None:
    assert tool._status_satisfies_expected("closed", "rma_sent") is True
    assert tool._status_satisfies_expected("rma_sent", "rma_sent") is True
    assert tool._status_satisfies_expected("ready_for_export", "rma_sent") is False


def test_wait_for_case_accepts_explicit_compatible_status(monkeypatch) -> None:
    class Client:
        def data(self, method, path, **kwargs):
            if path == "/api/v1/emails/44":
                return {"parse_results": [{"ticket_id": 55}]}
            if path == "/api/v1/tickets/55":
                return {
                    "ticket": {"id": 55, "current_status_code": "ready_for_export"},
                    "reply_records": [],
                }
            return {}

    value = tool._wait_for_case(
        Client(),
        44,
        "need_customer_info",
        0,
        timeout_seconds=1,
        accepted_statuses={"ready_for_export"},
    )

    assert value["ticket_detail"]["ticket"]["current_status_code"] == "ready_for_export"


def test_wait_for_case_fails_fast_when_followup_makes_no_progress(monkeypatch) -> None:
    class Client:
        def data(self, method, path, **kwargs):
            if path == "/api/v1/emails/44":
                return {"parse_results": [{"ticket_id": 55}]}
            if path == "/api/v1/tickets/55":
                return {
                    "ticket": {"id": 55, "current_status_code": "auto_replied"},
                    "reply_records": [
                        {"reply_type": "missing_fields", "send_status": "sent"},
                        {"reply_type": "missing_fields", "send_status": "sent"},
                    ],
                }
            return {}

    with pytest.raises(tool.GoldCliError) as exc:
        tool._wait_for_case(
            Client(),
            44,
            "rma_sent",
            2,
            timeout_seconds=1,
            max_sent_followups=1,
        )

    assert exc.value.code == "FOLLOWUP_NO_PROGRESS"


def test_wait_for_case_outbound_retries_for_imap_visibility(monkeypatch) -> None:
    rows = [[], [{"in_reply_to": "<original@accotest.com>"}]]
    monkeypatch.setattr(tool, "_rmatest2_new_messages", lambda _uid: rows.pop(0))
    monkeypatch.setattr(tool, "_assert_config_matches", lambda *_args: None)
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)

    result = tool._wait_for_case_outbound(
        10,
        original_message_id="<original@accotest.com>",
        expected_count=1,
        client=object(),
        expected_switches={},
        timeout_seconds=1,
    )

    assert len(result) == 1


def test_assert_case_reads_contact_values_from_versioned_pdf_snapshot() -> None:
    item = {
        "message_id": "<original@accotest.com>",
        "gold": {
            "expected_intent": "new_repair",
            "expected_subtype": None,
            "create_ticket": True,
            "expected_final_status": "rma_sent",
            "expected_final_fields": {
                "contact_person": "Liu Jiali",
                "contact_phone": "18200517485",
            },
            "missing_fields": [],
            "expected_items": [],
            "expected_outbound_count": 0,
        },
    }
    value = {
        "email_detail": {
            "email": {
                "intent_type": "new_repair",
                "handling_level": "auto_repair",
                "persistence_tier": "business",
            }
        },
        "ticket_detail": {
            "ticket": {
                "current_status_code": "rma_sent",
                "contact_person": "Liu Jiali",
                "contact_phone": "18200517485",
                "missing_fields": {},
            },
            "items": [],
            "reply_records": [
                {
                    "reply_type": "rma_authorization",
                    "send_status": "sent",
                    "template_id": 1,
                    "rma_pdf_data_snapshot": {
                        "template_version": "v3.1",
                        "data": {
                            "mailing_contact_person": "Liu Jiali",
                            "mailing_contact_phone": "18200517485",
                        },
                    },
                }
            ],
        },
    }

    assert tool._assert_case(item, value, []) == []
