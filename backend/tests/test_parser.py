from __future__ import annotations

from types import SimpleNamespace

from app.services.parser import classify_email, extract_fields


def test_rule_parser_handles_board_repair_email_candidates() -> None:
    body = """
新增1块板卡需要送修，请帮忙出RMA

联系电话：15262386889

维修板卡SN
Name
M81222006148042

故障现象：
calibration fail

[cid:image001.png@01D9E4DA.98E3A700]
"""
    email = SimpleNamespace(
        id=1,
        subject="8200板卡送修-20260625",
        text_body=body,
        html_body=None,
        clean_body=None,
        latest_reply_segment=None,
        from_address="AnsonNiu@innoscience.com",
        in_reply_to=None,
    )

    intent, confidence, _reason = classify_email(email, body)
    extracted = extract_fields(email)

    assert intent == "new_repair"
    assert confidence == 0.8
    assert extracted["fields"]["contact_email"] == "AnsonNiu@innoscience.com"
    assert extracted["fields"]["contact_phone"] == "15262386889"
    assert "calibration fail" in extracted["fields"]["problem_description"]
    assert extracted["items"] == [
        {"line_no": 1, "sn": "M81222006148042", "failure_description": "calibration fail"}
    ]
    assert extracted["missing_fields"] == {}
    assert extracted["conflict_fields"] == {}
    assert "NAME" not in extracted["evidence"]["sn_matches"]
