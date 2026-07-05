from app.models import Base


def test_phase_one_table_count() -> None:
    assert len(Base.metadata.tables) == 26


def test_phase_one_table_names() -> None:
    assert set(Base.metadata.tables) == {
        "users",
        "roles",
        "user_roles",
        "oss_objects",
        "email_threads",
        "emails",
        "email_attachments",
        "email_ticket_links",
        "repair_tickets",
        "repair_ticket_items",
        "workflow_statuses",
        "workflow_transitions",
        "ticket_status_logs",
        "field_audit_logs",
        "parse_results",
        "sn_validation_results",
        "sn_assets",
        "board_cards",
        "reply_templates",
        "reply_records",
        "manual_review_tasks",
        "notification_events",
        "ai_call_logs",
        "operation_logs",
        "system_event_logs",
        "job_run_logs",
    }


def test_parse_result_apply_status_columns() -> None:
    columns = Base.metadata.tables["parse_results"].columns
    assert "apply_status" in columns
    assert "applied_by_user_id" in columns
    assert "applied_at" in columns

