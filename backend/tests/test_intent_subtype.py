from app.core.email_classification import normalize_intent


def test_irrelevant_is_not_a_current_business_intent() -> None:
    assert normalize_intent("irrelevant") == "unknown"


def test_current_repair_intent_is_preserved() -> None:
    assert normalize_intent("new_repair") == "new_repair"


def test_unknown_value_is_normalized_to_unknown() -> None:
    assert normalize_intent("not_in_catalog") == "unknown"


def test_legacy_value_is_only_adapted_when_explicitly_allowed() -> None:
    assert normalize_intent("customer_reply") == "customer_supplement"
    assert normalize_intent("customer_reply", allow_legacy=False) == "unknown"
