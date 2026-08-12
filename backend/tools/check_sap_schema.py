from __future__ import annotations

import asyncio

from sqlalchemy import inspect, text

EXPECTED_REVISION = "q4l9g0b1c2d3"
EXPECTED_BUSINESS_TABLE_COUNT = 37

EXPECTED = {
    "export_sap": {
        "columns": {
            "ticket_id",
            "ticket_item_id",
            "relay_export_id",
            "source_request_id",
            "payload_hash",
            "remote_call_id",
            "rma_no",
            "customer_code",
            "shipping_fee",
            "repair_fee",
            "tax_rate",
            "charge_status",
        },
        "unique": {
            "uk_export_sap_source_request_id",
            "uk_export_sap_remote_call_id",
            "uk_export_sap_item_snapshot",
        },
        "foreign_keys": {
            "fk_export_sap_ticket",
            "fk_export_sap_ticket_item",
            "fk_export_sap_relay_export",
        },
    },
    "customer_service_policies": {
        "columns": {
            "customer_code",
            "policy_type",
            "charge_status",
            "customer_scope",
            "effective_from",
            "effective_until",
            "repair_price",
            "currency",
            "tax_rate",
            "shipping_fee_text",
            "reply_salutation",
            "hide_company_name",
            "force_manual_review",
            "enabled",
        },
        "unique": {"uk_customer_service_policies_code"},
        "foreign_keys": {"fk_customer_service_policies_imported_by"},
    },
    "board_cards": {
        "columns": {
            "board_code",
            "board_name",
            "return_location",
            "route_type",
            "customer_scope",
            "material_code",
            "need_ship_to_beijing",
        },
        "unique": set(),
        "foreign_keys": {"fk_board_cards_imported_by"},
    },
    "repair_tickets": {
        "columns": {
            "customer_scope",
            "customer_scope_source",
            "charge_status",
            "charge_status_source",
            "service_policy_id",
            "policy_resolution_status",
            "policy_snapshot",
        },
        "unique": {"uk_repair_tickets_no"},
        "foreign_keys": {
            "fk_repair_tickets_service_policy",
        },
    },
    "repair_ticket_items": {
        "columns": {
            "board_code",
            "board_name",
            "matched_board_card_id",
            "return_location",
            "return_address",
            "return_contact",
            "return_phone",
            "return_route_status",
            "return_route_snapshot",
        },
        "unique": {"uk_ticket_items_line"},
        "foreign_keys": {
            "fk_ticket_items_ticket",
            "fk_ticket_items_sn_asset",
            "fk_ticket_items_board_card",
        },
    },
    "ticket_rmas": {
        "columns": {
            "ticket_id",
            "rma_no",
            "customer_code",
            "repair_business_date",
            "status",
            "policy_snapshot",
            "pdf_oss_object_id",
            "reply_record_id",
            "received_at",
            "sent_at",
        },
        "unique": {"uk_ticket_rmas_ticket_no"},
        "foreign_keys": {
            "fk_ticket_rmas_ticket",
            "fk_ticket_rmas_pdf",
            "fk_ticket_rmas_reply",
        },
    },
    "ticket_rma_items": {
        "columns": {"ticket_rma_id", "ticket_item_id"},
        "unique": {"uk_ticket_rma_items_ticket_item"},
        "foreign_keys": {
            "fk_ticket_rma_items_rma",
            "fk_ticket_rma_items_item",
        },
    },
    "sap_sn_sync_batches": {
        "columns": {
            "batch_no",
            "status",
            "source_count",
            "valid_count",
            "duplicate_count",
            "count_change_percent",
            "snapshot_hash",
            "approved_by_user_id",
        },
        "unique": {"uk_sap_sn_sync_batches_no"},
        "foreign_keys": {"fk_sap_sn_sync_batches_approved_by"},
    },
    "sap_sn_staging": {
        "columns": {
            "sync_batch_id",
            "sn",
            "customer_code",
            "material_code",
            "values_json",
            "row_hash",
        },
        "unique": {"uk_sap_sn_staging_batch_sn"},
        "foreign_keys": {"fk_sap_sn_staging_batch"},
    },
}


def _verify(sync_connection) -> dict[str, object]:
    inspector = inspect(sync_connection)
    table_names = set(inspector.get_table_names())
    business_table_names = table_names - {"alembic_version"}
    errors: list[str] = []
    details: dict[str, object] = {}
    for table_name, expected in EXPECTED.items():
        if table_name not in table_names:
            errors.append(f"{table_name}:missing_table")
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        unique = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
            if constraint.get("name")
        }
        foreign_keys = {
            constraint["name"]
            for constraint in inspector.get_foreign_keys(table_name)
            if constraint.get("name")
        }
        missing_columns = sorted(expected["columns"] - columns)
        missing_unique = sorted(expected["unique"] - unique)
        missing_foreign_keys = sorted(expected["foreign_keys"] - foreign_keys)
        if missing_columns:
            errors.append(f"{table_name}:missing_columns={','.join(missing_columns)}")
        if missing_unique:
            errors.append(f"{table_name}:missing_unique={','.join(missing_unique)}")
        if missing_foreign_keys:
            errors.append(f"{table_name}:missing_foreign_keys={','.join(missing_foreign_keys)}")
        details[table_name] = {
            "column_count": len(columns),
            "unique_constraints": sorted(unique),
            "foreign_keys": sorted(foreign_keys),
        }
    return {
        "business_table_count": len(business_table_names),
        "details": details,
        "errors": errors,
    }


async def main() -> None:
    from app.core.database import engine

    async with engine.connect() as connection:
        revision = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        policy_count = (
            await connection.execute(text("SELECT COUNT(*) FROM customer_service_policies"))
        ).scalar_one()
        annual_enabled_count = (
            await connection.execute(
                text(
                    "SELECT COUNT(*) FROM customer_service_policies "
                    "WHERE policy_type='annual_free' AND enabled=1"
                )
            )
        ).scalar_one()
        rma_status_count = (
            await connection.execute(
                text(
                    "SELECT COUNT(*) FROM workflow_statuses "
                    "WHERE status_code='rma_sent' AND enabled=1"
                )
            )
        ).scalar_one()
        enabled_close_transition_count = (
            await connection.execute(
                text(
                    "SELECT COUNT(*) FROM workflow_transitions "
                    "WHERE to_status_code='closed' AND enabled=1"
                )
            )
        ).scalar_one()
        neutral_template_count = (
            await connection.execute(
                text(
                    "SELECT COUNT(*) FROM reply_templates "
                    "WHERE template_type='neutral_base' AND enabled=1"
                )
            )
        ).scalar_one()
        result = await connection.run_sync(_verify)
    await engine.dispose()
    print(f"revision={revision}")
    print(f"business_table_count={result['business_table_count']}")
    print(f"customer_policy_count={policy_count}")
    print(f"annual_policy_enabled_count={annual_enabled_count}")
    print(f"rma_sent_status_count={rma_status_count}")
    print(f"enabled_close_transition_count={enabled_close_transition_count}")
    print(f"neutral_template_count={neutral_template_count}")
    for table_name, detail in result["details"].items():
        print(f"{table_name}={detail}")
    if revision != EXPECTED_REVISION:
        raise SystemExit(f"unexpected_revision={revision}")
    if result["business_table_count"] != EXPECTED_BUSINESS_TABLE_COUNT:
        raise SystemExit(f"unexpected_business_table_count={result['business_table_count']}")
    if result["errors"]:
        raise SystemExit(";".join(result["errors"]))
    if policy_count < 37:
        raise SystemExit(f"customer_policy_count_below_required_baseline={policy_count}")
    if annual_enabled_count != 0:
        raise SystemExit(f"annual_policy_must_be_inactive_until_dated={annual_enabled_count}")
    if rma_status_count != 1:
        raise SystemExit(f"rma_sent_status_missing={rma_status_count}")
    if enabled_close_transition_count != 1:
        raise SystemExit(
            f"unexpected_enabled_close_transition_count={enabled_close_transition_count}"
        )
    if neutral_template_count < 1:
        raise SystemExit("neutral_base_template_missing")
    print("sap_rma_schema=passed")


if __name__ == "__main__":
    asyncio.run(main())
