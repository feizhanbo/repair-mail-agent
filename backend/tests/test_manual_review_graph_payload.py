from app.services.manual_review import graph_edited_fields


def test_graph_edited_fields_ignores_review_metadata() -> None:
    assert graph_edited_fields({"note": "checked", "fixed_fields": ["sn"]}) == {}


def test_graph_edited_fields_accepts_nested_ticket_edits() -> None:
    assert graph_edited_fields(
        {"note": "checked", "edited_fields": {"customer_code": "C001", "unknown": "x"}}
    ) == {"customer_code": "C001"}


def test_graph_edited_fields_keeps_legacy_direct_allowed_fields() -> None:
    assert graph_edited_fields({"contact_email": "a@example.com", "note": "checked"}) == {
        "contact_email": "a@example.com"
    }
