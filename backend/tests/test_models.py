from app.models import Base, Email, RepairTicket
from app.services.tickets import EMAIL_FIELDS, TICKET_FIELDS


def test_phase_one_table_count() -> None:
    assert len(Base.metadata.tables) == 41


def test_ticket_serializer_fields_exist_on_ticket_model() -> None:
    assert not [field for field in TICKET_FIELDS if not hasattr(RepairTicket, field)]


def test_email_serializer_fields_exist_and_expose_persistence_contract() -> None:
    assert not [field for field in EMAIL_FIELDS if not hasattr(Email, field)]
    assert {"persistence_tier", "classification_locked"} <= set(EMAIL_FIELDS)


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
        "mailbox_sync_states",
        "email_outbox",
        "mail_delivery_events",
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
        "system_configs",
        "ticket_status_logs",
        "ticket_relay_exports",
        "ticket_rma_items",
        "ticket_rmas",
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
    assert table.columns["message_id"].nullable is True
    assert {"raw_eml_oss_object_id", "raw_eml_sha256", "internal_date", "raw_retention_mode"} <= set(table.columns.keys())


def test_mail_transport_foundation_contract() -> None:
    sync_columns = Base.metadata.tables["mailbox_sync_states"].columns
    assert {"uid_validity", "sync_mode", "last_discovered_uid", "last_fetched_uid", "lease_expires_at"} <= set(sync_columns.keys())
    outbox_columns = Base.metadata.tables["email_outbox"].columns
    assert {
        "reply_record_id", "idempotency_key", "message_id", "frozen_eml_oss_object_id",
        "frozen_eml_sha256", "status", "lease_owner", "accepted_at",
    } <= set(outbox_columns.keys())
    delivery_columns = Base.metadata.tables["mail_delivery_events"].columns
    assert {"outbox_id", "original_message_id", "final_recipient", "delivery_status"} <= set(delivery_columns.keys())


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
            "RequestID",
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
