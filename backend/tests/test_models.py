from app.models import Base


def test_phase_one_table_count() -> None:
    assert len(Base.metadata.tables) == 27


def test_phase_one_table_names() -> None:
    assert set(Base.metadata.tables) == {
        "ai_call_logs",
        "board_cards",
        "email_attachments",
        "email_threads",
        "email_ticket_links",
        "emails",
        "field_audit_logs",
        "job_run_logs",
        "mail_fetch_records",
        "manual_review_tasks",
        "notification_events",
        "operation_logs",
        "oss_objects",
        "parse_results",
        "repair_ticket_items",
        "repair_tickets",
        "reply_records",
        "reply_templates",
        "roles",
        "sn_assets",
        "sn_validation_results",
        "system_event_logs",
        "ticket_status_logs",
        "user_roles",
        "users",
        "workflow_statuses",
        "workflow_transitions",
    }


def test_parse_result_apply_status_columns() -> None:
    columns = Base.metadata.tables["parse_results"].columns
    assert "apply_status" in columns
    assert "applied_by_user_id" in columns
    assert "applied_at" in columns

