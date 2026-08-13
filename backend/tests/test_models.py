from app.models import Base


def test_phase_one_table_count() -> None:
    assert len(Base.metadata.tables) == 39


def test_phase_one_table_names() -> None:
    assert set(Base.metadata.tables) == {
        "ai_call_logs",
        "board_cards",
        "customer_service_policies",
        "email_attachments",
        "email_threads",
        "email_ticket_links",
        "emails",
        "export_sap",
        "external_sync_checkpoints",
        "external_operation_records",
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
        "sap_sn_sync_batches",
        "sap_sn_staging",
        "sn_validation_results",
        "system_event_logs",
        "ticket_status_logs",
        "ticket_relay_exports",
        "ticket_rma_items",
        "ticket_rmas",
        "user_roles",
        "users",
        "workflow_statuses",
        "workflow_transitions",
        "workflow_executions",
        "workflow_interrupts",
    }


def test_parse_result_apply_status_columns() -> None:
    columns = Base.metadata.tables["parse_results"].columns
    assert "apply_status" in columns
    assert "applied_by_user_id" in columns
    assert "applied_at" in columns


def test_workflow_interrupt_references_execution_business_identity() -> None:
    execution = Base.metadata.tables["workflow_executions"]
    interrupt = Base.metadata.tables["workflow_interrupts"]
    assert execution.c.execution_id.unique is None
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in execution.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("execution_id",) in unique_columns
    assert "checkpoint_id" in execution.columns
    assert "checkpoint_step" in execution.columns
    foreign_keys = {foreign_key.target_fullname for foreign_key in interrupt.c.execution_id.foreign_keys}
    assert foreign_keys == {"workflow_executions.execution_id"}
    assert "checkpoint_id" in interrupt.columns
    assert "checkpoint_step" in interrupt.columns


def test_workflow_audit_links_do_not_block_business_record_deletion() -> None:
    execution = Base.metadata.tables["workflow_executions"]
    interrupt = Base.metadata.tables["workflow_interrupts"]

    for column_name in ("email_id", "ticket_id", "trigger_job_id"):
        foreign_key = next(iter(execution.c[column_name].foreign_keys))
        assert foreign_key.ondelete == "SET NULL"

    for column_name in ("manual_task_id", "resumed_by_user_id"):
        foreign_key = next(iter(interrupt.c[column_name].foreign_keys))
        assert foreign_key.ondelete == "SET NULL"

    execution_foreign_key = next(iter(interrupt.c.execution_id.foreign_keys))
    assert execution_foreign_key.ondelete == "CASCADE"


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


def test_sn_hierarchy_and_sap_export_columns_exist() -> None:
    sn_columns = Base.metadata.tables["sn_assets"].columns
    assert {
        "service_tracking_card_no",
        "parent_sn",
        "top_sn",
        "parent_material_code",
        "top_material_code",
    } <= set(sn_columns.keys())
    export_columns = Base.metadata.tables["export_sap"].columns
    assert {
        "sn",
        "customer_code",
        "material_code",
        "customer_name",
        "material_name",
        "charge_status",
        "contact_person",
        "contact_phone",
        "email_subject",
        "problem_description",
        "repair_requested_at",
        "mailing_address",
        "currency",
        "shipping_fee",
        "repair_fee",
        "tax_rate",
        "ticket_id",
        "ticket_item_id",
        "relay_export_id",
            "source_request_id",
        "payload_hash",
        "remote_call_id",
        "rma_no",
    } <= set(export_columns.keys())


def test_board_policy_and_ticket_route_columns_exist() -> None:
    board_columns = Base.metadata.tables["board_cards"].columns
    assert {
        "board_code",
        "board_name",
        "return_location",
        "route_type",
        "customer_scope",
    } <= set(board_columns.keys())
    policy_columns = Base.metadata.tables["customer_service_policies"].columns
    assert {"charge_status", "customer_scope"} <= set(policy_columns.keys())
    ticket_columns = Base.metadata.tables["repair_tickets"].columns
    assert {
        "customer_scope",
        "charge_status",
        "policy_resolution_status",
        "policy_snapshot",
    } <= set(ticket_columns.keys())
    item_columns = Base.metadata.tables["repair_ticket_items"].columns
    assert {
        "board_code",
        "board_name",
        "matched_board_card_id",
        "return_location",
        "return_address",
        "return_route_status",
        "return_route_snapshot",
    } <= set(item_columns.keys())
