from __future__ import annotations

from types import SimpleNamespace

from app.services.business_rules import required_missing_for_ticket, required_missing_for_values


def test_new_repair_missing_fields_use_business_required_matrix() -> None:
    missing = required_missing_for_values(
        intent_type="new_repair",
        fields={
            "customer_name": "测试客户",
            "contact_person": "测试联系人",
            "contact_email": "rmatest2@accotest.com",
            "request_date": "2026-07-20",
            "mailing_address": "测试地址",
            "problem_description": "测试故障",
            "contact_phone": None,
            "customer_code": None,
        },
        items=[{"sn": "TESTSN0001"}],
        reported_missing={"contact_phone": "缺少电话", "customer_code": "缺少客户编码"},
    )

    assert missing == {}


def test_non_business_intents_never_create_customer_missing_fields() -> None:
    for intent in ("normal_reply", "device_received", "rma_sent", "irrelevant", "unknown"):
        assert required_missing_for_values(
            intent_type=intent,
            fields={},
            items=[],
            reported_missing={"contact_phone": "缺少电话", "sn": "缺少 SN"},
        ) == {}


def test_ticket_required_matrix_requires_sn_but_not_phone_or_customer_code() -> None:
    ticket = SimpleNamespace(
        customer_name="测试客户",
        contact_person="测试联系人",
        contact_email="rmatest2@accotest.com",
        request_date="2026-07-20",
        mailing_address="测试地址",
        problem_description="测试故障",
        contact_phone=None,
        customer_code=None,
    )

    assert required_missing_for_ticket(ticket, []) == {"sn": "缺少设备 SN，无法校验资产。"}

