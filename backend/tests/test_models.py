from app.models import Base


def test_phase_one_table_count() -> None:
    assert len(Base.metadata.tables) == 30


def test_phase_one_table_names() -> None:
    assert set(Base.metadata.tables) == {
        "ai_call_logs",
        "board_cards",
        "email_attachments",
        "email_threads",
        "email_ticket_links",
        "emails",
        "external_sync_checkpoints",
        "field_audit_logs",
        "job_run_logs",
        "mail_fetch_records",
        "manual_review_tasks",
        "notification_events",
        "notification_user_states",
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
        "ticket_relay_exports",
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


def test_mail_fetch_records_keep_uid_idempotency_constraint() -> None:
    table = Base.metadata.tables["mail_fetch_records"]
    constraint_columns = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.name
    }
    assert constraint_columns["uk_mail_fetch_records"] == (
        "mailbox_account",
        "folder_name",
        "uid_validity",
        "imap_uid",
    )
    assert "fetch_status" in table.columns


def test_email_oss_link_columns_exist_for_archival_consistency() -> None:
    email_columns = Base.metadata.tables["emails"].columns
    attachment_columns = Base.metadata.tables["email_attachments"].columns
    assert "raw_eml_oss_object_id" in email_columns
    assert "oss_object_id" in attachment_columns
