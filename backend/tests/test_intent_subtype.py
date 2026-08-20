from app.integrations.ai_provider import AiExtractResponse, _normalize_response_payload


def test_irrelevant_remains_the_existing_top_level_type() -> None:
    payload = _normalize_response_payload({"intent_type": "irrelevant"}, AiExtractResponse)
    assert payload["intent_type"] == "irrelevant"


def test_existing_repair_type_is_not_reclassified_as_irrelevant() -> None:
    repair = _normalize_response_payload({"intent_type": "new_repair"}, AiExtractResponse)
    assert repair["intent_type"] == "new_repair"


def test_unknown_type_is_not_reclassified_as_irrelevant() -> None:
    payload = _normalize_response_payload({"intent_type": "not_in_catalog"}, AiExtractResponse)
    assert payload["intent_type"] == "unknown"
