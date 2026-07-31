from __future__ import annotations

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.services.mail_test_preflight import REQUIRED_DATABASE_REVISION
from tools.run_new_repair_mail_e2e import E2EFailure, assert_database_preflight, validate_complete_path, validate_missing_path


def _reply(reply_type: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": 31,
        "reply_type": reply_type,
        "send_status": "sent",
        "to_addresses": "rmatest2@accotest.com",
        "cc_addresses": None,
        "subject": "[TEST ONLY] controlled message",
    }
    value.update(overrides)
    return value


def test_preflight_required_revision_matches_unique_alembic_head() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [REQUIRED_DATABASE_REVISION]


def test_real_e2e_preflight_requires_current_database_and_zero_messages() -> None:
    assert_database_preflight(
        {
            "status": "passed",
            "messages_sent": 0,
            "database": {
                "status": "ready",
                "current_revision": REQUIRED_DATABASE_REVISION,
                "required_revision": REQUIRED_DATABASE_REVISION,
            },
            "smtp": {"stage": "complete", "messages_sent": 0},
        }
    )
    with pytest.raises(E2EFailure, match="Database revision"):
        assert_database_preflight(
            {
                "status": "passed",
                "messages_sent": 0,
                "database": {"status": "failed", "current_revision": "old", "required_revision": "head"},
                "smtp": {"stage": "complete", "messages_sent": 0},
            }
        )


def test_complete_path_requires_one_atomic_test_rma_reply() -> None:
    parent_message_id = "<complete@example.test>"
    reply = _reply(
        "rma_authorization",
        subject="Re: controlled message",
        in_reply_to=parent_message_id,
        references_header=parent_message_id,
        rma_pdf_oss_object_id=44,
        rma_pdf_data_snapshot={"watermark": "TEST ONLY"},
    )
    result = validate_complete_path(
        {
            "email_detail": {
                "email": {
                    "intent_type": "new_repair",
                    "message_id": parent_message_id,
                }
            },
            "ticket_detail": {
                "ticket": {
                    "id": 7,
                    "current_status_code": "rma_sent",
                    "rma_status": "sent",
                    "missing_fields": {},
                    "customer_code": "TEST-CUSTOMER",
                    "safety_check_hash": "a" * 64,
                    "sn_validation_hash": "b" * 64,
                },
                "thread": {"id": 11},
                "items": [{"sn": "TEST-SN", "material_code": "TEST-MATERIAL"}],
                "rma_records": [{"id": 3, "status": "sent"}],
                "reply_records": [reply],
            },
        }
    )
    assert result["reply"] == reply


def test_missing_path_allows_actual_required_field_subset_and_no_attachment() -> None:
    parent_message_id = "<missing@example.test>"
    reply = _reply(
        "missing_fields",
        subject="Re: controlled message",
        in_reply_to=parent_message_id,
        references_header=parent_message_id,
        missing_fields={"mailing_address": "required"},
    )
    result = validate_missing_path(
        {
            "email_detail": {
                "email": {
                    "intent_type": "new_repair",
                    "message_id": parent_message_id,
                }
            },
            "ticket_detail": {
                "ticket": {
                    "id": 8,
                    "current_status_code": "auto_replied",
                    "followup_count": 1,
                    "missing_fields": {"mailing_address": "required"},
                },
                "thread": {"id": 12},
                "reply_records": [reply],
            },
        }
    )
    assert result["reply"] == reply


def test_missing_path_rejects_optional_phone_question() -> None:
    with pytest.raises(E2EFailure, match="customer-actionable required fields"):
        validate_missing_path(
            {
                "email_detail": {"email": {"intent_type": "new_repair"}},
                "ticket_detail": {
                    "ticket": {
                        "current_status_code": "auto_replied",
                        "followup_count": 1,
                        "missing_fields": {"mailing_address": "required", "contact_phone": "required"},
                    },
                    "reply_records": [_reply("missing_fields")],
                },
            }
        )
