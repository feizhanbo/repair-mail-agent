from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.integrations.ai_provider import (
    AiExtractResponse,
    AiReplyDraftResponse,
    DeepSeekProvider,
    _normalize_response_payload,
)
from app.core.repair_items import normalize_repair_item, normalize_repair_items
from app.models import AiCallLog, Email, EmailAttachment
from app.services.ai import (
    _apply_request_date_fallback,
    _enrich_ai_quality,
    _key_result,
    _merge_attachment_business_data,
    _status_for,
    ai_log_diagnostics,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_ai_extract_schema_accepts_sample_output() -> None:
    parsed = AiExtractResponse.model_validate(
        {
            "intent_type": "new_repair",
            "extracted_fields": {"contact_email": "customer@example.com", "problem_description": "设备无法开机"},
            "extracted_items": [{"line_no": 1, "sn": "SN202607040001", "failure_description": "无法开机"}],
            "missing_fields": {},
            "conflict_fields": {},
            "confidence_score": 0.86,
            "field_confidences": {"sn": 0.9, "problem_description": 0.8},
            "evidence": {"sn": "邮件正文第 2 行"},
        }
    )

    assert parsed.intent_type == "new_repair"
    assert parsed.extracted_items[0]["sn"] == "SN202607040001"
    assert _status_for(parsed, None) == "success"


def test_ai_extract_schema_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        AiExtractResponse.model_validate({"confidence_score": 1.2})


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [("customer_reply", "customer_supplement"), ("internal_forward", "repair_thread_other"), ("invented", "unknown")],
)
def test_ai_intent_taxonomy_is_normalized(legacy: str, expected: str) -> None:
    normalized = _normalize_response_payload({"intent_type": legacy}, AiExtractResponse)
    assert normalized["intent_type"] == expected


def test_deepseek_payload_normalization_handles_common_shape_drift() -> None:
    normalized = _normalize_response_payload(
        {
            "extracted_fields": None,
            "extracted_items": {"items": [{"sn": "SN001"}]},
            "missing_fields": ["contact_phone"],
            "conflict_fields": None,
            "confidence_score": 86,
            "field_confidences": {"sn": "92", "contact_phone": None},
            "confidence_reasons": "SN present",
            "original_evidence": "SN001",
        },
        AiExtractResponse,
    )
    parsed = AiExtractResponse.model_validate(normalized)
    assert parsed.extracted_items == [{"sn": "SN001"}]
    assert parsed.missing_fields == {}
    assert parsed.confidence_score == 0.86
    assert parsed.field_confidences == {"sn": 0.92}
    assert parsed.confidence_reasons == ["SN present"]


def test_ai_item_aliases_are_normalized_before_ticket_mapping() -> None:
    normalized = _normalize_response_payload(
        {
            "intent_type": "new_repair",
            "extracted_items": [
                {"serial_number": "P80012205200178", "fault_description": "Channel failure"},
                {"device_sn": "P80012205200179", "problem_description": "Cannot start"},
            ],
        },
        AiExtractResponse,
    )
    assert normalized["extracted_items"][0]["sn"] == "P80012205200178"
    assert normalized["extracted_items"][0]["failure_description"] == "Channel failure"
    assert normalized["extracted_items"][1]["sn"] == "P80012205200179"
    assert normalized["extracted_items"][1]["failure_description"] == "Cannot start"


def test_excel_row_number_never_wins_over_part_serial_number() -> None:
    normalized = _normalize_response_payload(
        {
            "intent_type": "new_repair",
            "extracted_items": [
                {
                    "serial_number": "1",
                    "part_serial_no": "M8123260108000171",
                    "failure_description": "Controlled failure",
                }
            ],
        },
        AiExtractResponse,
    )

    assert normalized["extracted_items"][0]["line_no"] == 1
    assert normalized["extracted_items"][0]["sn"] == "M8123260108000171"


def test_repair_item_normalization_deduplicates_canonical_sn() -> None:
    normalized = normalize_repair_items(
        [
            {"serial_number": "1", "part_serial_no": "M8123260108000110"},
            {"line_no": 2, "sn": "m8123260108000110", "failure_description": "FAIL"},
        ]
    )

    assert len(normalized) == 1
    assert normalized[0]["sn"] == "M8123260108000110"
    assert normalized[0]["failure_description"] == "FAIL"
    assert normalize_repair_item({"serial_number": "2"}) == {"serial_number": "2", "line_no": 2}


def test_structured_xlsx_fields_fill_ai_omissions_without_making_phone_required() -> None:
    parsed = AiExtractResponse(
        intent_type="new_repair",
        extracted_fields={"customer_name": "Test Customer"},
        extracted_items=[],
        missing_fields={"contact_phone": "optional field incorrectly requested", "sn": "required"},
        confidence_score=0.95,
    )
    attachment = EmailAttachment(
        id=30,
        email_id=16,
        file_name="controlled.xlsx",
        parse_status="parsed",
        extracted_json={
            "extracted_fields": {
                "customer_name": "Test Customer",
                "contact_person": "Test Contact",
                "phone": "13800000000",
                "request_date": "2026-07-20",
                "return_address": "Fictitious Test Address",
            },
            "extracted_items": [
                {
                    "serial_number": "1",
                    "part_serial_no": "M8123260108000171",
                    "failure_description": "Controlled failure",
                }
            ],
        },
    )

    merged = _merge_attachment_business_data(parsed, [attachment])

    assert merged.extracted_fields["mailing_address"] == "Fictitious Test Address"
    assert merged.extracted_fields["problem_description"] == "Controlled failure"
    assert merged.extracted_fields["contact_phone"] == "13800000000"
    assert merged.extracted_items[0]["sn"] == "M8123260108000171"
    assert merged.extracted_items[0]["line_no"] == 1
    assert merged.evidence["structured_attachment_source_ids"] == [30]


def test_multi_sn_merge_compares_canonical_sets_not_row_order() -> None:
    parsed = AiExtractResponse(
        intent_type="new_repair",
        extracted_items=[
            {"sn": "M8123260108000118"},
            {"sn": "M8123260108000110"},
            {"sn": "M8123260108000169"},
        ],
        confidence_score=0.95,
    )
    attachment = EmailAttachment(
        id=31,
        email_id=17,
        file_name="multi.xlsx",
        parse_status="parsed",
        extracted_json={
            "extracted_items": [
                {"serial_number": "1", "part_serial_no": "M8123260108000110"},
                {"serial_number": "2", "part_serial_no": "M8123260108000169"},
                {"serial_number": "3", "part_serial_no": "M8123260108000118"},
            ]
        },
    )

    merged = _merge_attachment_business_data(parsed, [attachment])

    assert "sn" not in merged.conflict_fields
    assert {item["sn"] for item in merged.extracted_items} == {
        "M8123260108000110",
        "M8123260108000169",
        "M8123260108000118",
    }


def test_multi_sn_merge_keeps_real_set_difference_as_conflict() -> None:
    parsed = AiExtractResponse(
        intent_type="new_repair",
        extracted_items=[{"sn": "M8123260108000171"}],
        confidence_score=0.95,
    )
    attachment = EmailAttachment(
        id=32,
        email_id=18,
        file_name="different.xlsx",
        parse_status="parsed",
        extracted_json={"extracted_items": [{"part_serial_no": "M8123260108000110"}]},
    )

    merged = _merge_attachment_business_data(parsed, [attachment])

    assert merged.conflict_fields["sn"] == "AI extraction conflicts with deterministic attachment parsing."


def test_request_date_fallback_prefers_explicit_then_sent_then_received() -> None:
    email = Email(
        id=51,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        sent_at=datetime(2026, 7, 24, 9, 58),
        received_at=datetime(2026, 7, 24, 10, 0),
    )
    explicit_fields = {"request_date": "2026-07-20"}
    explicit_evidence: dict = {}
    explicit_confidence: dict = {}
    _apply_request_date_fallback(
        fields=explicit_fields,
        evidence=explicit_evidence,
        field_confidences=explicit_confidence,
        source_email=email,
    )
    assert explicit_fields["request_date"] == "2026-07-20"
    assert "derived_fields" not in explicit_evidence

    sent_fields: dict = {}
    sent_evidence: dict = {}
    sent_confidence: dict = {}
    _apply_request_date_fallback(
        fields=sent_fields,
        evidence=sent_evidence,
        field_confidences=sent_confidence,
        source_email=email,
    )
    assert sent_fields["request_date"] == "2026-07-24"
    assert sent_evidence["derived_fields"]["request_date"]["source"] == "email_sent_at"

    email.sent_at = None
    received_fields: dict = {}
    received_evidence: dict = {}
    received_confidence: dict = {}
    _apply_request_date_fallback(
        fields=received_fields,
        evidence=received_evidence,
        field_confidences=received_confidence,
        source_email=email,
    )
    assert received_fields["request_date"] == "2026-07-24"
    assert received_evidence["derived_fields"]["request_date"]["source"] == "email_received_at"


def test_customer_supplement_keeps_existing_ticket_request_date() -> None:
    email = Email(
        id=51,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        sent_at=datetime(2026, 7, 24, 9, 58),
    )
    fields = {"request_date": "2026-07-25"}
    evidence: dict = {}
    confidences: dict = {}

    _apply_request_date_fallback(
        fields=fields,
        evidence=evidence,
        field_confidences=confidences,
        source_email=email,
        existing_request_date=date(2026, 7, 24),
    )

    assert fields["request_date"] == "2026-07-24"
    assert evidence["derived_fields"]["request_date"]["source"] == "existing_ticket"


@pytest.mark.anyio
async def test_missing_field_email_uses_failure_description_and_email_date() -> None:
    class Session:
        async def scalar(self, _statement):
            return SimpleNamespace(asset_status="valid")

    email = Email(
        id=51,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        sent_at=datetime(2026, 7, 24, 9, 58),
    )
    parsed = AiExtractResponse(
        intent_type="new_repair",
        extracted_fields={},
        extracted_items=[
            {"sn": "M81231611200106", "failure_description": "自检FAIL"},
            {"sn": "M81232011400127", "failure_description": "自检FAIL"},
            {"sn": "M81162102260019", "failure_description": "校准FAIL"},
        ],
        missing_fields={
            "request_date": "missing",
            "contact_email": "missing",
            "customer_name": "missing",
            "contact_person": "missing",
            "mailing_address": "missing",
            "contact_phone": "optional",
        },
        confidence_score=0.85,
    )

    enriched = await _enrich_ai_quality(Session(), parsed=parsed, email=email, attachments=[])

    assert enriched.extracted_fields["request_date"] == "2026-07-24"
    assert enriched.extracted_fields["contact_email"] == "rmatest2@accotest.com"
    assert enriched.extracted_fields["problem_description"] == "自检FAIL\n校准FAIL"
    assert set(enriched.missing_fields) == {"customer_name", "contact_person", "mailing_address"}


@pytest.mark.anyio
async def test_customer_name_uses_unanimous_valid_sn_asset_master_data() -> None:
    class Session:
        async def scalar(self, _statement):
            return SimpleNamespace(
                asset_status="valid",
                customer_code="E2E-CBIT-20260804",
                customer_name="上海林众电子科技有限公司",
            )

    email = Email(
        id=71,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        sent_at=datetime(2026, 7, 29, 10, 57),
    )
    parsed = AiExtractResponse(
        intent_type="new_repair",
        extracted_fields={
            "contact_person": "test contact",
            "contact_email": "rmatest2@accotest.com",
            "mailing_address": "test mailing address",
            "problem_description": "CBIT128 upgrade",
        },
        extracted_items=[
            {"sn": "M81072420200031", "failure_description": "CBIT128 upgrade"},
            {"sn": "M81072420200030", "failure_description": "CBIT128 upgrade"},
        ],
        missing_fields={"customer_name": "missing"},
        confidence_score=0.85,
    )

    enriched = await _enrich_ai_quality(
        Session(), parsed=parsed, email=email, attachments=[]
    )

    assert enriched.extracted_fields["customer_name"] == "上海林众电子科技有限公司"
    assert enriched.extracted_fields["customer_code"] == "E2E-CBIT-20260804"
    assert enriched.evidence["derived_fields"]["customer_name"] == {
        "source": "sn_asset_consensus",
        "sn_count": 2,
    }
    assert enriched.missing_fields == {}


def test_ai_reply_schema_accepts_sample_output() -> None:
    parsed = AiReplyDraftResponse.model_validate(
        {
            "subject": "请补充报修信息：RMA001",
            "body": "您好，请补充设备 SN 和故障现象。",
            "missing_fields": {"sn": "缺少设备 SN"},
            "confidence_score": 0.78,
            "risk_level": "low",
            "suggestions": ["人工审核后发送"],
        }
    )

    assert parsed.subject.startswith("请补充")
    assert _status_for(parsed, None) == "success"


def test_ai_log_key_result_keeps_summary_not_sensitive_values() -> None:
    parsed = AiExtractResponse.model_validate(
        {
            "intent_type": "new_repair",
            "extracted_fields": {"contact_email": "customer@example.com"},
            "extracted_items": [{"sn": "SN-SENSITIVE-001"}],
            "missing_fields": {"mailing_address": "缺少邮寄地址"},
            "conflict_fields": {},
            "confidence_score": 0.91,
            "field_confidences": {},
            "evidence": {"snippet": "SN-SENSITIVE-001"},
        }
    )

    key_result = _key_result("field_extract", parsed)
    serialized = json.dumps(key_result, ensure_ascii=False)
    assert key_result == {
        "intent_type": "new_repair",
        "field_keys": ["contact_email"],
        "item_count": 1,
        "missing_field_keys": ["mailing_address"],
        "conflict_field_keys": [],
    }
    assert "customer@example.com" not in serialized
    assert "SN-SENSITIVE-001" not in serialized


def test_ai_log_diagnostics_describes_model_stage_reason_and_action() -> None:
    ai_log = AiCallLog(
        trace_id="trace-1",
        call_type="attachment_visual_parse",
        provider_name="qwen",
        model_name="qwen-vl-plus",
        prompt_version="v1",
        status="failed",
        error_code="QWEN_PROVIDER_TIMEOUT",
        log_file_path="logs/ai.jsonl",
    )

    diagnostics = ai_log_diagnostics(ai_log)

    assert diagnostics["ai_stage"] == "Qwen 图片/PDF 多模态解析"
    assert "qwen/qwen-vl-plus" in diagnostics["problem_description"]
    assert "超时" in diagnostics["problem_reason"]
    assert diagnostics["resolution_suggestion"]


@pytest.mark.anyio
async def test_deepseek_request_payload_does_not_persist_api_key() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-test-key"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent_type": "unknown",
                                    "extracted_fields": {},
                                    "extracted_items": [],
                                    "missing_fields": {},
                                    "conflict_fields": {},
                                    "confidence_score": 0.4,
                                    "field_confidences": {},
                                    "evidence": {},
                                }
                            )
                        }
                    }
                ]
            },
        )

    provider = DeepSeekProvider(
        api_key="secret-test-key",
        base_url="https://api.deepseek.example",
        model="deepseek-v4-flash",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    completion = await provider.chat_json(messages=[{"role": "user", "content": "return JSON"}], response_model=AiExtractResponse)

    assert "secret-test-key" not in json.dumps(completion.request_payload)
    assert _status_for(completion.parsed, None) == "low_confidence"
