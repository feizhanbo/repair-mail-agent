from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.integrations.ai_provider import (
    AiExtractResponse,
    AiReplyDraftResponse,
    _normalize_response_payload,
)
from app.config import settings
from app.integrations.llm_gateway import public_llm_routes
from app.core.repair_items import normalize_repair_item, normalize_repair_items
from app.models import AiCallLog, Email, EmailAttachment, EmailThread, RepairTicket
from app.services.ai import (
    _apply_request_date_fallback,
    _can_auto_recover_customer_supplement,
    _enrich_ai_quality,
    _apply_deterministic_supplement_fields,
    _key_result,
    _merge_attachment_business_data,
    _status_for,
    ai_log_diagnostics,
    _normalize_customer_mailing_address,
    _problem_description_from_latest_reply,
    create_ai_parse_candidate,
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


def test_problem_description_falls_back_to_explicit_latest_reply_failure() -> None:
    email = Email(
        text_body=(
            "I am preparing to send SVI40 which is detected FAIL on selfcheck.\n"
            "Contact: Example Person\n"
            "Please find attached file, as I will send FAIL log."
        )
    )

    result = _problem_description_from_latest_reply(email)

    assert result is not None
    assert "detected FAIL" in result
    assert "Contact" not in result


def test_problem_description_fallback_ignores_negated_problem_statement() -> None:
    email = Email(text_body="No issue was found during selfcheck.")

    assert _problem_description_from_latest_reply(email) is None


@pytest.mark.anyio
async def test_field_extraction_quality_uses_locked_preclassification_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def scalar(self, _statement):
            return SimpleNamespace(
                sn="M81252101025023",
                asset_status="valid",
                customer_code="E2EJP006",
                customer_name="Example Japan Inc.",
                material_code="M8125",
                material_name="SVI40",
            )

        def add(self, value: object) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            return None

    parsed = AiExtractResponse(
        intent_type="unknown",
        extracted_fields={},
        extracted_items=[{"sn": "M81252101025023"}],
        missing_fields={},
        confidence_score=0.9,
    )
    ai_log = SimpleNamespace(
        id=9,
        trace_id="trace-9",
        provider_name="deepseek",
        model_name="deepseek-chat",
        route_name="repair_field_extract",
        fallback_used=False,
    )

    async def run_ai_json(*_args, **_kwargs):
        return parsed, ai_log

    monkeypatch.setattr("app.services.ai._run_ai_json", run_ai_json)
    email = Email(
        id=73,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        intent_type="new_repair",
        classification_reason_code="customer_initiated_repair_request",
        clean_body=(
            "I am preparing to send SVI40 which is detected FAIL on selfcheck.\n"
            "SN M81252101025023"
        ),
        sent_at=datetime(2026, 8, 12, 14, 27),
    )

    result = await create_ai_parse_candidate(
        Session(), email=email, attachments=[], mode="repair_field_extract"
    )

    assert result is not None
    candidate = result["parse_result"]
    assert candidate.intent_type == "new_repair"
    assert candidate.extracted_fields["problem_description"] == (
        "I am preparing to send SVI40 which is detected FAIL on selfcheck."
    )
    assert "problem_description" not in candidate.missing_fields


def test_customer_mailing_address_removes_only_adjacent_municipality_duplicate() -> None:
    assert (
        _normalize_customer_mailing_address(
            "上海市上海市松江区香闵路1188弄1-4号"
        )
        == "上海市松江区香闵路1188弄1-4号"
    )
    assert (
        _normalize_customer_mailing_address("江苏省徐州市上海路1号")
        == "江苏省徐州市上海路1号"
    )


@pytest.mark.anyio
async def test_enrichment_rejects_signature_only_return_fields_and_repairs_shifted_sn_columns() -> None:
    assets = {
        "M81232105400093": SimpleNamespace(
            sn="M81232105400093",
            asset_status="valid",
            customer_code="JSICAT",
            customer_name="江苏爱矽半导体科技有限公司",
            material_code="A8200B31327",
            material_name="STS8200B FOVI",
        ),
        "M81172009050036Y": SimpleNamespace(
            sn="M81172009050036Y",
            asset_status="valid",
            customer_code="JSICAT",
            customer_name="江苏爱矽半导体科技有限公司",
            material_code="A8200B30057",
            material_name="STS8200B DIO",
        ),
    }

    class Session:
        async def scalar(self, statement):
            params = statement.compile().params
            value = next(iter(params.values()), "")
            return assets.get(str(value).upper())

    email = Email(
        id=72,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        sent_at=datetime(2026, 8, 12, 14, 24),
        clean_body=(
            "需要返厂维修，请帮忙提供下地址。\n"
            "STS8200BA8200B31327FOVIM81232105400093校准FAIL\n"
            "STS8200BA8200B30057DIOM81172009050036Y校准FAIL\n\n"
            "张跃 测试助理工程师\n江苏爱矽半导体科技有限公司\n"
            "地址：徐州经济技术开发区\n手机：15298760948"
        ),
    )
    parsed = AiExtractResponse(
        intent_type="new_repair",
        extracted_fields={
            "customer_name": "江苏爱矽半导体科技有限公司",
            "contact_person": "张跃",
            "contact_phone": "15298760948",
            "mailing_address": "徐州经济技术开发区",
            "problem_description": "校准FAIL",
        },
        extracted_items=[
            {"sn": "A8200B31327", "board_code": "M81232105400093", "board_name": "FOVI", "failure_description": "校准FAIL"},
            {"sn": "A8200B30057", "board_code": "M81172009050036", "board_name": "DIO", "failure_description": "校准FAIL"},
        ],
        missing_fields={},
        confidence_score=0.85,
    )

    enriched = await _enrich_ai_quality(Session(), parsed=parsed, email=email, attachments=[])

    assert [row["sn"] for row in enriched.extracted_items] == [
        "M81232105400093",
        "M81172009050036Y",
    ]
    assert enriched.extracted_fields["customer_code"] == "JSICAT"
    assert [row["material_code"] for row in enriched.extracted_items] == [
        "A8200B31327",
        "A8200B30057",
    ]
    assert not {
        "contact_person",
        "contact_phone",
        "mailing_address",
    } & set(enriched.extracted_fields)
    assert set(enriched.missing_fields) == {
        "contact_person",
        "contact_phone",
        "mailing_address",
    }


@pytest.mark.anyio
async def test_enrichment_keeps_explicit_english_post_repair_address_block() -> None:
    class Session:
        async def scalar(self, _statement):
            return SimpleNamespace(
                sn="M81252101025023",
                asset_status="valid",
                customer_code="JP001",
                customer_name="Example Japan Inc.",
                material_code="M8125",
                material_name="SVI40",
            )

    email = Email(
        id=73,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        sent_at=datetime(2026, 8, 12, 14, 27),
        clean_body=(
            "Regarding to shipping information after repaired, please send back to Japan office.\n"
            "Teruhiko Kodama\nExample Japan Inc.\n"
            "Addr : #601, Shouan 3-20-11, Suginami, Tokyo\n"
            "TEL : +81-3-6312-2251\nSN M81252101025023"
        ),
    )
    parsed = AiExtractResponse(
        intent_type="new_repair",
        extracted_fields={
            "customer_name": "Example Japan Inc.",
            "contact_person": "Teruhiko Kodama",
            "contact_phone": "+81-3-6312-2251",
            "mailing_address": "#601 Shouan 3-20-11, Suginami, Tokyo",
            "problem_description": "selfcheck FAIL",
        },
        extracted_items=[
            {"sn": "M81252101025023", "failure_description": "selfcheck FAIL"}
        ],
        missing_fields={},
        confidence_score=0.75,
        evidence={"manual_review_direction": "Please verify the return address."},
        manual_review_direction="Please verify the return address.",
    )

    enriched = await _enrich_ai_quality(Session(), parsed=parsed, email=email, attachments=[])

    assert enriched.extracted_fields["mailing_address"] == "#601, Shouan 3-20-11, Suginami, Tokyo"
    assert enriched.extracted_items[0]["material_code"] == "M8125"
    assert "mailing_address" not in enriched.missing_fields
    assert enriched.confidence_score == pytest.approx(0.85)
    assert enriched.manual_review_direction is None
    assert enriched.evidence["quality_controls"][
        "explicit_customer_return_context"
    ]["allowed"] is True


@pytest.mark.anyio
async def test_enrichment_replaces_generic_ai_address_with_explicit_addr_line() -> None:
    class Session:
        async def scalar(self, _statement):
            return SimpleNamespace(
                sn="M81252101025023",
                asset_status="valid",
                customer_code="JP001",
                customer_name="Example Japan Inc.",
                material_code="M8125",
                material_name="SVI40",
            )

    email = Email(
        id=74,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        sent_at=datetime(2026, 8, 12, 14, 27),
        clean_body=(
            "Regarding to shipping information after repaired, please send back to Japan office.\n"
            "Teruhiko Kodama\nExample Japan Inc.\n"
            "Addr : #601, Shouan 3-20-11, Suginami, Tokyo\n"
            "ZIP : 167-0054\n"
            "TEL : +81-3-6312-2251\nSN M81252101025023"
        ),
    )
    parsed = AiExtractResponse(
        intent_type="new_repair",
        extracted_fields={
            "customer_name": "Example Japan Inc.",
            "contact_person": "Teruhiko Kodama",
            "contact_phone": "+81-3-6312-2251",
            "mailing_address": "Japan office",
            "problem_description": "selfcheck FAIL",
        },
        extracted_items=[
            {"sn": "M81252101025023", "failure_description": "selfcheck FAIL"}
        ],
        missing_fields={},
        confidence_score=0.75,
    )

    enriched = await _enrich_ai_quality(
        Session(), parsed=parsed, email=email, attachments=[]
    )

    assert enriched.extracted_fields["mailing_address"] == (
        "#601, Shouan 3-20-11, Suginami, Tokyo ZIP: 167-0054"
    )
    assert enriched.field_confidences["mailing_address"] == 1.0
    assert enriched.evidence["derived_fields"]["mailing_address"] == {
        "source": "explicit_english_address_label"
    }
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
    # Shape normalization has no intent context; the business-required matrix
    # adds contact_phone after classification/enrichment.
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


def test_customer_supplement_explicit_phone_overrides_ai_omission() -> None:
    email = Email(
        id=52,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        latest_reply_segment="寄回联系电话：18200517485",
    )
    fields: dict = {}
    evidence: dict = {}
    confidences: dict = {}

    _apply_deterministic_supplement_fields(
        fields=fields,
        email=email,
        evidence=evidence,
        field_confidences=confidences,
    )

    assert fields["contact_phone"] == "18200517485"
    assert confidences["contact_phone"] == 1.0
    assert evidence["derived_fields"]["contact_phone"]["source"] == "explicit_supplement_label"


@pytest.mark.anyio
async def test_complete_linked_supplement_ignores_generic_model_review_advice() -> None:
    source_email = Email(
        id=50,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        sent_at=datetime(2026, 8, 12, 14, 26),
    )
    ticket = RepairTicket(
        id=91,
        ticket_no="E2E-91",
        source_email_id=source_email.id,
        request_date=date(2026, 8, 12),
        missing_fields={"contact_phone": "missing"},
    )
    thread = EmailThread(id=71, thread_key="e2e-thread-71", ticket_id=ticket.id)
    email = Email(
        id=52,
        thread_id=thread.id,
        mailbox_account="rmatest1@accotest.com",
        from_address="rmatest2@accotest.com",
        latest_reply_segment="寄回联系电话：18200517485",
        sent_at=datetime(2026, 8, 12, 14, 30),
    )
    asset = SimpleNamespace(
        sn="M81232504500155",
        asset_status="valid",
        customer_code="E2E-JOULWATT-20260805",
        customer_name="Example Customer",
        material_code="Z.SM.8123V120A",
        material_name="FOVI100",
    )

    class Session:
        async def get(self, model, identity):
            return {
                (EmailThread, thread.id): thread,
                (RepairTicket, ticket.id): ticket,
                (Email, source_email.id): source_email,
            }.get((model, identity))

        async def scalar(self, _statement):
            return asset

        async def execute(self, _statement):
            item = SimpleNamespace(
                line_no=1,
                sn=asset.sn,
                material_code=asset.material_code,
                material_name=asset.material_name,
                board_code=None,
                board_name=None,
                failure_description="selfcheck FAIL",
                id=501,
            )

            class Result:
                def scalars(self):
                    return self

                def all(self):
                    return [item]

            return Result()

    parsed = AiExtractResponse(
        intent_type="customer_supplement",
        extracted_fields={},
        extracted_items=[{"sn": asset.sn, "failure_description": "selfcheck FAIL"}],
        missing_fields={"contact_phone": "missing"},
        conflict_fields={"sn": "VIM81232504500155: asset not found"},
        confidence_score=0.75,
        evidence={"manual_review_direction": "Please manually verify the customer reply."},
        manual_review_direction="Please manually verify the customer reply.",
    )

    enriched = await _enrich_ai_quality(
        Session(), parsed=parsed, email=email, attachments=[]
    )

    assert enriched.extracted_fields["contact_phone"] == "18200517485"
    assert enriched.missing_fields == {}
    assert enriched.conflict_fields == {}
    assert [item["sn"] for item in enriched.extracted_items] == [asset.sn]
    assert enriched.evidence["quality_controls"][
        "customer_supplement_item_preservation"
    ]["item_count"] == 1
    assert enriched.confidence_score == pytest.approx(0.85)
    assert enriched.manual_review_direction is None
    assert "manual_review_direction" not in enriched.evidence
    assert enriched.evidence["quality_controls"][
        "customer_supplement_auto_recovery"
    ] == {
        "allowed": True,
        "reason": "linked_ticket_complete_without_conflicts",
        "ticket_id": 91,
        "resolved_field_keys": ["contact_phone"],
    }


@pytest.mark.parametrize(
    ("existing_ticket", "expected_missing_fields", "missing", "conflicts"),
    [
        (None, {"contact_phone"}, {}, {}),
        (RepairTicket(id=92, ticket_no="E2E-92"), set(), {}, {}),
        (RepairTicket(id=93, ticket_no="E2E-93"), {"contact_phone"}, {"contact_phone": "missing"}, {}),
        (RepairTicket(id=94, ticket_no="E2E-94"), {"contact_phone"}, {}, {"contact_phone": "conflict"}),
    ],
)
def test_supplement_does_not_auto_recover_without_all_safety_conditions(
    existing_ticket: RepairTicket | None,
    expected_missing_fields: set[str],
    missing: dict[str, str],
    conflicts: dict[str, str],
) -> None:
    assert not _can_auto_recover_customer_supplement(
        intent_type="customer_supplement",
        existing_ticket=existing_ticket,
        expected_missing_fields=expected_missing_fields,
        missing=missing,
        conflicts=conflicts,
    )


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
    assert set(enriched.missing_fields) == {
        "customer_name",
        "contact_person",
        "contact_phone",
        "mailing_address",
    }


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
            "contact_phone": "18286702632",
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


def test_llm_route_public_contract_does_not_persist_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_API_KEY", "secret-test-key")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "secret-qwen-key")
    payload = json.dumps(public_llm_routes())
    assert "secret-test-key" not in payload
    assert "secret-qwen-key" not in payload
