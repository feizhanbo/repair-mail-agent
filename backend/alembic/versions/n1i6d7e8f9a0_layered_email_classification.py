"""layered email classification and remove device receipt workflow

Revision ID: n1i6d7e8f9a0
Revises: m0h5c6d7e8f9
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "n1i6d7e8f9a0"
down_revision: str | None = "m0h5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CLASSIFICATION_COLUMNS = (
    ("handling_level", sa.String(30)),
    ("classification_version", sa.String(50)),
    ("classification_confidence", mysql.DECIMAL(5, 4)),
    ("classification_reason_code", sa.String(100)),
)


def _column_names(table: str) -> set[str]:
    if op.get_context().as_sql:
        if table == "repair_tickets":
            return {
                "device_received_at", "device_received_source", "device_received_email_id",
                "device_received_note", "device_received_idempotency_key", "device_receipt_ack_status",
            }
        return set()
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _index_names(table: str) -> set[str]:
    if op.get_context().as_sql:
        return {"idx_repair_tickets_device_ack_status"} if table == "repair_tickets" else set()
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}


def _foreign_key_names(table: str) -> set[str]:
    if op.get_context().as_sql:
        if table == "manual_review_tasks":
            return {"fk_manual_tasks_ticket"}
        if table == "repair_tickets":
            return {"fk_repair_tickets_device_received_email"}
        return set()
    return {item["name"] for item in sa.inspect(op.get_bind()).get_foreign_keys(table) if item.get("name")}


def upgrade() -> None:
    for table in ("emails", "parse_results"):
        for name, column_type in CLASSIFICATION_COLUMNS:
            if name not in _column_names(table):
                op.add_column(table, sa.Column(name, column_type, nullable=True))
    if op.get_context().as_sql or "idx_emails_handling_intent" not in _index_names("emails"):
        op.create_index("idx_emails_handling_intent", "emails", ["handling_level", "intent_type"])
    if op.get_context().as_sql or "idx_parse_results_handling_intent" not in _index_names("parse_results"):
        op.create_index("idx_parse_results_handling_intent", "parse_results", ["handling_level", "intent_type"])

    op.execute("UPDATE emails SET handling_level='auto_repair', classification_version='legacy-mapped', classification_reason_code='LEGACY_EXACT_MAPPING' WHERE intent_type IN ('new_repair','customer_supplement')")
    op.execute("UPDATE parse_results SET handling_level='auto_repair', classification_version='legacy-mapped', classification_reason_code='LEGACY_EXACT_MAPPING', classification_confidence=confidence_score WHERE intent_type IN ('new_repair','customer_supplement')")
    op.execute("UPDATE emails SET intent_type='device_intake_received', intent_subtype=NULL, handling_level='lifecycle_only', classification_version='legacy-mapped', classification_reason_code='LEGACY_DEVICE_RECEIVED_MAPPING' WHERE intent_type='device_received'")
    op.execute("UPDATE parse_results SET intent_type='device_intake_received', intent_subtype=NULL, handling_level='lifecycle_only', classification_version='legacy-mapped', classification_reason_code='LEGACY_DEVICE_RECEIVED_MAPPING', classification_confidence=confidence_score WHERE intent_type='device_received'")
    op.execute("UPDATE emails SET handling_level='unknown', classification_version='legacy-mapped', classification_reason_code='LEGACY_UNKNOWN_MAPPING' WHERE intent_type IS NULL OR intent_type='unknown'")
    op.execute("UPDATE parse_results SET handling_level='unknown', classification_version='legacy-mapped', classification_reason_code='LEGACY_UNKNOWN_MAPPING', classification_confidence=confidence_score WHERE intent_type IS NULL OR intent_type='unknown'")
    op.execute("UPDATE emails SET classification_version='legacy', classification_reason_code='LEGACY_RECLASSIFICATION_REQUIRED' WHERE classification_version IS NULL")
    op.execute("UPDATE parse_results SET classification_version='legacy', classification_reason_code='LEGACY_RECLASSIFICATION_REQUIRED', classification_confidence=confidence_score WHERE classification_version IS NULL")

    if "thread_id" not in _column_names("manual_review_tasks"):
        op.add_column("manual_review_tasks", sa.Column("thread_id", mysql.BIGINT(unsigned=True), nullable=True))
    if op.get_context().as_sql or "idx_manual_tasks_thread" not in _index_names("manual_review_tasks"):
        op.create_index("idx_manual_tasks_thread", "manual_review_tasks", ["thread_id"])
    if op.get_context().as_sql or "fk_manual_tasks_thread" not in _foreign_key_names("manual_review_tasks"):
        op.create_foreign_key("fk_manual_tasks_thread", "manual_review_tasks", "email_threads", ["thread_id"], ["id"], ondelete="SET NULL")
    if "fk_manual_tasks_ticket" in _foreign_key_names("manual_review_tasks"):
        op.drop_constraint("fk_manual_tasks_ticket", "manual_review_tasks", type_="foreignkey")
    op.alter_column("manual_review_tasks", "ticket_id", existing_type=mysql.BIGINT(unsigned=True), nullable=True)
    if op.get_context().as_sql or "fk_manual_tasks_ticket" not in _foreign_key_names("manual_review_tasks"):
        op.create_foreign_key("fk_manual_tasks_ticket", "manual_review_tasks", "repair_tickets", ["ticket_id"], ["id"], ondelete="SET NULL")

    # MySQL error 3823 forbids this column in a CHECK while its FK uses
    # ON DELETE SET NULL. The service layer enforces email-or-ticket context.

    ticket_columns = (
        sa.Column("ticket_category", sa.String(30), nullable=False, server_default="standard_repair"),
        sa.Column("origin_handling_level", sa.String(30), nullable=True),
        sa.Column("origin_intent_type", sa.String(50), nullable=True),
        sa.Column("resolved_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("resolution_code", sa.String(100), nullable=True),
        sa.Column("resolution_summary", sa.String(500), nullable=True),
    )
    for column in ticket_columns:
        if column.name not in _column_names("repair_tickets"):
            op.add_column("repair_tickets", column)
    if op.get_context().as_sql or "idx_repair_tickets_category_status" not in _index_names("repair_tickets"):
        op.create_index("idx_repair_tickets_category_status", "repair_tickets", ["ticket_category", "current_status_code"])
    op.execute("UPDATE repair_tickets SET origin_handling_level='auto_repair', origin_intent_type=(SELECT intent_type FROM emails WHERE emails.id=repair_tickets.source_email_id) WHERE origin_handling_level IS NULL")

    op.execute(
        """
        INSERT INTO operation_logs
            (user_id, correlation_id, email_id, ticket_id, operation_type, target_type,
             target_id, description, before_data, after_data, created_at)
        SELECT NULL, NULL, device_received_email_id, id,
               'legacy_device_receipt_archived', 'repair_ticket', id,
               'Device receipt feature removed; historical facts archived before column removal.',
               NULL,
               JSON_OBJECT(
                   'device_received_at', device_received_at,
                   'device_received_source', device_received_source,
                   'device_received_email_id', device_received_email_id,
                   'device_received_note', device_received_note,
                   'device_received_idempotency_key', device_received_idempotency_key,
                   'device_receipt_ack_status', device_receipt_ack_status
               ), CURRENT_TIMESTAMP(3)
        FROM repair_tickets
        WHERE (
               device_received_at IS NOT NULL
            OR device_received_email_id IS NOT NULL
            OR device_receipt_ack_status <> 'not_received'
        )
          AND NOT EXISTS (
              SELECT 1 FROM operation_logs existing
              WHERE existing.operation_type='legacy_device_receipt_archived'
                AND existing.target_type='repair_ticket'
                AND existing.target_id=repair_tickets.id
          )
        """
    )
    op.execute(
        """
        INSERT IGNORE INTO email_ticket_links
            (email_id, ticket_id, link_type, link_reason, linked_by_user_id, created_at)
        SELECT device_received_email_id, id, 'lifecycle_event',
               'Archived legacy device receipt relation', NULL, CURRENT_TIMESTAMP(3)
        FROM repair_tickets WHERE device_received_email_id IS NOT NULL
        """
    )
    op.execute("UPDATE reply_templates SET enabled=0 WHERE template_type='device_received_ack'")
    op.execute("UPDATE email_threads t JOIN repair_tickets r ON r.id=t.ticket_id SET t.ticket_id=NULL WHERE r.current_status_code='closed'")

    if "idx_repair_tickets_device_ack_status" in _index_names("repair_tickets"):
        op.drop_index("idx_repair_tickets_device_ack_status", table_name="repair_tickets")
    if "fk_repair_tickets_device_received_email" in _foreign_key_names("repair_tickets"):
        op.drop_constraint("fk_repair_tickets_device_received_email", "repair_tickets", type_="foreignkey")
    for column in (
        "device_received_at", "device_received_source", "device_received_email_id",
        "device_received_note", "device_received_idempotency_key", "device_receipt_ack_status",
    ):
        if column in _column_names("repair_tickets"):
            op.drop_column("repair_tickets", column)

    op.execute(
        """
        INSERT INTO workflow_statuses
            (status_code, status_name, status_category, description, is_terminal, sort_order, enabled)
        SELECT 'resolved', '人工业务已完成', 'terminal',
               'SECOND人工业务已处理并记录结果，不代表签发RMA。', 1, 95, 1
        WHERE NOT EXISTS (SELECT 1 FROM workflow_statuses WHERE status_code='resolved')
        """
    )
    op.execute(
        """
        INSERT INTO workflow_transitions
            (from_status_code, to_status_code, trigger_event, condition_desc, require_manual, enabled)
        SELECT 'manual_review', 'resolved', 'manual_business_resolved',
               'SECOND人工业务已通过现有业务渠道处理并记录结果。', 1, 1
        WHERE NOT EXISTS (
            SELECT 1 FROM workflow_transitions
            WHERE from_status_code='manual_review' AND to_status_code='resolved'
              AND trigger_event='manual_business_resolved'
        )
          AND EXISTS (SELECT 1 FROM workflow_statuses WHERE status_code='manual_review')
          AND EXISTS (SELECT 1 FROM workflow_statuses WHERE status_code='resolved')
        """
    )
    op.execute("UPDATE workflow_transitions SET enabled=0 WHERE to_status_code='closed' AND NOT (from_status_code='rma_sent' AND trigger_event='rma_issued_and_archived')")


def downgrade() -> None:
    op.execute("DELETE FROM workflow_transitions WHERE from_status_code='manual_review' AND to_status_code='resolved' AND trigger_event='manual_business_resolved'")
    op.execute("DELETE FROM workflow_statuses WHERE status_code='resolved'")

    op.add_column("repair_tickets", sa.Column("device_received_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("repair_tickets", sa.Column("device_received_source", sa.String(30), nullable=True))
    op.add_column("repair_tickets", sa.Column("device_received_email_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("repair_tickets", sa.Column("device_received_note", sa.Text(), nullable=True))
    op.add_column("repair_tickets", sa.Column("device_received_idempotency_key", sa.String(100), nullable=True))
    op.add_column("repair_tickets", sa.Column("device_receipt_ack_status", sa.String(30), nullable=False, server_default="not_received"))
    op.create_foreign_key("fk_repair_tickets_device_received_email", "repair_tickets", "emails", ["device_received_email_id"], ["id"])
    op.create_index("idx_repair_tickets_device_ack_status", "repair_tickets", ["device_receipt_ack_status", "updated_at"])

    op.drop_index("idx_repair_tickets_category_status", table_name="repair_tickets")
    for column in ("resolution_summary", "resolution_code", "resolved_at", "origin_intent_type", "origin_handling_level", "ticket_category"):
        op.drop_column("repair_tickets", column)

    op.drop_constraint("fk_manual_tasks_thread", "manual_review_tasks", type_="foreignkey")
    op.drop_index("idx_manual_tasks_thread", table_name="manual_review_tasks")
    op.drop_column("manual_review_tasks", "thread_id")
    op.drop_constraint("fk_manual_tasks_ticket", "manual_review_tasks", type_="foreignkey")
    op.execute("DELETE FROM manual_review_tasks WHERE ticket_id IS NULL")
    op.alter_column("manual_review_tasks", "ticket_id", existing_type=mysql.BIGINT(unsigned=True), nullable=False)
    op.create_foreign_key("fk_manual_tasks_ticket", "manual_review_tasks", "repair_tickets", ["ticket_id"], ["id"], ondelete="CASCADE")

    op.drop_index("idx_parse_results_handling_intent", table_name="parse_results")
    op.drop_index("idx_emails_handling_intent", table_name="emails")
    for table in ("parse_results", "emails"):
        for name, _column_type in reversed(CLASSIFICATION_COLUMNS):
            op.drop_column(table, name)
