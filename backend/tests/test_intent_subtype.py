from app.integrations.ai_provider import AiExtractResponse, _normalize_response_payload


def test_irrelevant_defaults_to_general_subtype() -> None:
    payload = _normalize_response_payload({"intent_type": "irrelevant"}, AiExtractResponse)
    assert payload["intent_subtype"] == "general_irrelevant"


def test_out_of_scope_subtype_is_preserved_only_for_irrelevant() -> None:
    payload = _normalize_response_payload(
        {"intent_type": "irrelevant", "intent_subtype": "out_of_scope_repair"},
        AiExtractResponse,
    )
    assert payload["intent_subtype"] == "out_of_scope_repair"

    repair = _normalize_response_payload(
        {"intent_type": "new_repair", "intent_subtype": "out_of_scope_repair"},
        AiExtractResponse,
    )
    assert repair["intent_subtype"] is None


def test_unknown_irrelevant_subtype_is_not_accepted() -> None:
    payload = _normalize_response_payload(
        {"intent_type": "irrelevant", "intent_subtype": "missing_sn"},
        AiExtractResponse,
    )
    assert payload["intent_subtype"] == "general_irrelevant"
