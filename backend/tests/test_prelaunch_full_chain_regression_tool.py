from __future__ import annotations

import copy

import yaml

from tools import run_prelaunch_full_chain_regression as tool


def test_boundary_is_fixed_to_requested_shanghai_timestamp() -> None:
    assert tool.BOUNDARY.isoformat() == "2026-09-20T12:00:00+08:00"


def test_document_section_contains_every_business_stage() -> None:
    stages = {
        "imap": {"status": "passed"},
        "classification": {"status": "passed", "intent_type": "new_repair"},
        "parsing": {"status": "passed"},
        "matching": {"status": "passed"},
        "validation": {"status": "passed"},
        "sap_simulation": {"status": "passed"},
        "outbound_email": {"status": "passed"},
        "closure": {"status": "passed"},
        "manual_intervention": {"status": "not_required"},
    }
    result = {
        "run_id": "20260920-test",
        "status": "passed",
        "boundary": tool.BOUNDARY.isoformat(),
        "max_sends": 5,
        "summary": {
            "mail_count": 1,
            "closed_count": 1,
            "manual_case_count": 0,
            "smtp_sent_count": 1,
        },
        "cases": [
            {
                "message_id": "<test@example.com>",
                "subject": "repair",
                "ticket_no": "T1",
                "issues": [],
                "stages": stages,
            }
        ],
        "issues": [],
    }

    text = tool._document_section(result)

    for label in ("分类", "解析", "匹配/校验", "SAP 模拟", "邮件", "闭单", "人工痕迹"):
        assert label in text
    assert "<test@example.com>" in text
    assert "```json" not in text
    assert "result.json" in text


def test_pruned_reply_excludes_mail_body() -> None:
    value = tool._pruned_reply(
        {
            "id": 1,
            "send_status": "sent",
            "smtp_message_id": "<out@example.com>",
            "final_body": "sensitive body",
            "final_html_body": "<p>sensitive</p>",
        }
    )

    assert value["send_status"] == "sent"
    assert "final_body" not in value
    assert "final_html_body" not in value


def test_nonvisual_routes_use_qwen_37_plus_with_qwen_38_fallback() -> None:
    routes = yaml.safe_load(
        (tool.BACKEND_ROOT / "config" / "llm_routes.yaml").read_text(encoding="utf-8")
    )["tasks"]

    for task in ("mail_classification", "repair_field_extract"):
        assert routes[task]["primary"] == {"profile": "qwen", "model": "qwen3.7-plus"}
        assert routes[task]["fallback"] == {"profile": "qwen", "model": "qwen3.8-flash"}
    assert routes["attachment_text_parse"]["primary"]["model"] == "qwen3.7-plus"
    assert routes["attachment_text_parse"]["fallback"] == {"profile": "qwen", "model": "qwen3.8-flash"}
    assert routes["attachment_visual_parse"]["primary"]["model"] == "qwen-vl-plus"
    assert routes["reply_draft"]["primary"] == {"profile": "qwen", "model": "qwen3.7-plus"}
    assert routes["reply_draft"]["fallback"] == {"profile": "qwen", "model": "qwen3.8-flash"}


def test_stability_report_detects_stable_business_output() -> None:
    case = {
        "message_id": "<stable@example.com>",
        "ticket_created": True,
        "no_ticket_reason": None,
        "ai_token_usage": [
            {
                "call_type": "mail_classification",
                "model": "qwen3.7-plus",
                "input_tokens": 4,
                "output_tokens": 6,
                "total_tokens": 10,
            }
        ],
        "stages": {
            "classification": {"status": "passed", "intent_type": "new_repair"},
            "parsing": {"status": "passed", "parse_result": {"extracted_fields": {"serial_no": "S1"}}},
            "manual_intervention": {"tasks": []},
            "closure": {"status": "passed", "ticket_status": "closed"},
        },
    }
    runs = [
        {"run_id": f"run-{index}", "status": "passed", "cases": [case], "token_usage": {"total_tokens": 10}}
        for index in range(1, 4)
    ]

    report = tool._stability_report("batch", runs)

    assert report["stable"] is True
    assert report["token_usage"]["total_tokens"] == 30


def _ai_evidence_fixture() -> tuple[dict, list[dict]]:
    result = {
        "run_id": "run-evidence",
        "started_at": "2026-09-23T08:00:00",
        "finished_at": "2026-09-23T16:01:00+08:00",
        "cases": [
            {
                "message_id": "<evidence@example.com>",
                "email_id": 10,
                "ticket_id": None,
                "ai_token_usage": [
                    {
                        "call_type": "mail_classification",
                        "model": "qwen3.7-plus",
                        "status": "success",
                        "error_code": None,
                    }
                ],
                "stages": {"parsing": {"attachments": []}},
            }
        ],
    }
    records = [
        {
            "trace_id": "trace-evidence",
            "call_type": "mail_classification",
            "provider": "qwen",
            "model": "qwen3.7-plus",
            "prompt_version": "prompt-v1",
            "schema_version": "schema-v1",
            "parser_version": "parser-v1",
            "structured_output_method": "json_schema",
            "route_name": "qwen",
            "route_attempt": 1,
            "fallback_used": False,
            "status": "success",
            "error_code": None,
            "created_at": "2026-09-23T08:00:30",
            "token_usage": {"input": 10, "output": 5, "total": 15},
            "input_payload": {
                "message_id": "<evidence@example.com>",
                "body": "完整客户正文",
                "api_key": "must-not-leak",
            },
            "request_payload": {
                "messages": [
                    {"role": "system", "content": "完整系统提示词"},
                    {"role": "user", "content": "完整客户正文"},
                ],
                "image": "data:image/png;base64,AAAA",
            },
            "response_payload": {
                "choices": [{"message": {"content": "{\"intent\":\"new_repair\"}"}}]
            },
            "parsed_result": {"intent": "new_repair", "confidence": 0.98},
        }
    ]
    return result, records


def test_ai_evidence_enrichment_preserves_text_and_redacts_secrets() -> None:
    result, records = _ai_evidence_fixture()

    enriched = tool._enrich_result_with_ai_evidence(result, records)

    assert enriched["ai_evidence_summary"]["status"] == "complete"
    assert enriched["ai_evidence_summary"]["expected_call_count"] == 1
    call = enriched["cases"][0]["ai_calls"][0]
    assert call["input"]["body"] == "完整客户正文"
    assert call["input"]["api_key"] == "[REDACTED]"
    assert call["request"]["messages"][0]["content"] == "完整系统提示词"
    assert call["request"]["image"]["binary_ref"] is True
    assert "AAAA" not in str(call["request"]["image"])
    assert call["response"]["choices"][0]["message"]["content"] == '{"intent":"new_repair"}'
    assert call["parsed_result"] == {"intent": "new_repair", "confidence": 0.98}


def test_ai_evidence_enrichment_is_idempotent() -> None:
    result, records = _ai_evidence_fixture()

    first = tool._enrich_result_with_ai_evidence(copy.deepcopy(result), records)
    second = tool._enrich_result_with_ai_evidence(copy.deepcopy(first), records)

    assert second == first


def test_ai_evidence_enrichment_marks_missing_call_incomplete() -> None:
    result, _ = _ai_evidence_fixture()

    enriched = tool._enrich_result_with_ai_evidence(result, [])

    summary = enriched["ai_evidence_summary"]
    assert summary["status"] == "incomplete"
    assert summary["captured_call_count"] == 0
    assert summary["missing_calls"][0]["call_type"] == "mail_classification"


def test_enrich_ai_command_requires_batch_id() -> None:
    args = tool.build_parser().parse_args(["enrich-ai", "--batch-id", "batch-1"])

    assert args.command == "enrich-ai"
    assert args.batch_id == "batch-1"
