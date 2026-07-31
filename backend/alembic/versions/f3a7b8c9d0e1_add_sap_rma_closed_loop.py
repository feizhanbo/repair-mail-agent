"""add SAP/RMA closed loop, customer policies and explicit recovery state

Revision ID: f3a7b8c9d0e1
Revises: e2f6a7b8c9d0
Create Date: 2026-07-28
"""

from collections.abc import Sequence

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "f3a7b8c9d0e1"
down_revision: str | None = "e2f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FREE_POLICIES = (
    ("CM00159", "华润赛美科微电子（深圳）有限公司"),
    ("CM01269", "深圳市国微电子有限公司"),
    ("CM00198", "合肥通富微电子有限公司"),
    ("CM00326", "合肥通富微电子有限公司"),
    ("CM00250", "通富微电子股份有限公司"),
    ("CM01465", "通富通科（南通）微电子有限公司"),
    ("CM00280", "希荻微电子集团股份有限公司"),
    ("CM00126", "长电科技（宿迁）有限公司"),
    ("CM00127", "长电科技（滁州）有限公司"),
    ("CM00123", "江阴长电先进封装有限公司"),
    ("CM00124", "江苏长电科技股份有限公司"),
    ("CM02011", "长电科技(江阴)有限公司"),
    ("CM02020", "长电科技汽车电子（上海）有限公司"),
    ("CM01479", "芯迈半导体技术（杭州）股份有限公司"),
    ("CM00136", "南京矽力微电子技术有限公司"),
    ("CM00076", "矽力杰半导体技术（杭州）有限公司"),
    ("CM00278", "常州银河世纪微电子股份有限公司"),
    ("CM01518", "无锡市宜欣科技有限公司"),
    ("CM01934", "杰华特微电子（珠海）有限公司"),
    ("CM01214", "杰华特微电子股份有限公司"),
)

ANNUAL_POLICIES = (
    ("CM00414", "无锡芯启博电子有限公司"),
    ("CM00331", "江阴市华拓芯片测试有限公司"),
    ("CM00428", "华润微集成电路（无锡）有限公司"),
    ("CM00128", "江阴佳泰电子科技有限公司"),
    ("CM01243", "佛山市蓝箭电子股份有限公司"),
    ("CM00178", "天水华天科技股份有限公司"),
)

SPECIAL_POLICIES = (
    ("CM00148", "Chengdu Monolithic Power System", 1100, 13, "CNY"),
    ("CM00147", "宁芯（成都）集成电路封装测试有限公司", 1100, 13, "CNY"),
    ("CM00288", "宁芯（成都）集成电路封装测试有限公司", 1100, 13, "CNY"),
    ("CM00142", "扬州亿芯微电子有限公司", 1100, 6, "CNY"),
    ("CM00102", "苏州固锝电子股份有限公司", 1100, 6, "CNY"),
    ("CM00333", "达迩科技（成都）有限公司", 1100, 6, "CNY"),
    ("CM00337", "达迩科技（成都）有限公司", 1100, 6, "CNY"),
    ("CM00178", "天水华天科技股份有限公司", 1000, 13, "CNY"),
    ("CM00143", "华天科技（昆山）电子有限公司", 1170, 13, "CNY"),
    ("CM00315", "O2Micro International Ltd", 165, 13, "USD"),
)


def upgrade() -> None:
    if context.is_offline_mode():
        op.execute(
            "-- PRECONDITION: export_sap must contain zero legacy rows before applying "
            "f3a7b8c9d0e1"
        )
    else:
        bind = op.get_bind()
        existing_export_rows = bind.execute(sa.text("SELECT COUNT(*) FROM export_sap")).scalar_one()
        if existing_export_rows:
            raise RuntimeError(
                "export_sap contains legacy rows; migrate or archive them before applying f3a7b8c9d0e1"
            )

    op.add_column(
        "emails",
        sa.Column("processing_stage", sa.String(length=50), server_default="fetched", nullable=False),
    )
    op.add_column("emails", sa.Column("terminal_reason_code", sa.String(length=100), nullable=True))
    op.add_column("emails", sa.Column("last_error_code", sa.String(length=100), nullable=True))
    op.add_column(
        "emails",
        sa.Column("retryable", mysql.TINYINT(display_width=1), server_default=sa.text("1"), nullable=False),
    )
    op.add_column("emails", sa.Column("next_retry_at", mysql.DATETIME(fsp=3), nullable=True))
    op.execute(
        """
        UPDATE emails
        SET processing_stage = CASE
                WHEN parse_status IN ('parsed', 'skipped') THEN 'completed'
                WHEN parse_status IN ('needs_manual', 'failed') THEN 'manual_review'
                WHEN parse_status = 'parsing' THEN 'parsing'
                ELSE 'fetched'
            END,
            terminal_reason_code = CASE
                WHEN parse_status = 'parsed' THEN 'EMAIL_PROCESSING_COMPLETED'
                WHEN parse_status = 'skipped' THEN 'EMAIL_PROCESSING_SKIPPED'
                WHEN parse_status = 'needs_manual' THEN 'EMAIL_REQUIRES_MANUAL_REVIEW'
                WHEN parse_status = 'failed' THEN 'EMAIL_PROCESSING_FAILED'
                ELSE NULL
            END,
            retryable = CASE
                WHEN parse_status IN ('parsed', 'skipped') THEN 0
                ELSE 1
            END
        """
    )

    op.add_column("repair_tickets", sa.Column("terminal_reason_code", sa.String(length=100), nullable=True))
    op.add_column("repair_tickets", sa.Column("terminal_reason", sa.String(length=500), nullable=True))
    op.add_column("repair_tickets", sa.Column("closed_at", mysql.DATETIME(fsp=3), nullable=True))

    op.add_column(
        "export_sap",
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=False),
    )
    op.add_column(
        "export_sap",
        sa.Column("ticket_item_id", mysql.BIGINT(unsigned=True), nullable=False),
    )
    op.add_column(
        "export_sap",
        sa.Column("relay_export_id", mysql.BIGINT(unsigned=True), nullable=False),
    )
    op.add_column("export_sap", sa.Column("ticket_version", sa.Integer(), nullable=False))
    op.add_column("export_sap", sa.Column("submission_key", sa.String(length=64), nullable=False))
    op.add_column("export_sap", sa.Column("payload_hash", mysql.CHAR(length=64), nullable=False))
    op.add_column("export_sap", sa.Column("policy_snapshot", mysql.JSON(), nullable=True))
    op.add_column(
        "export_sap",
        sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
    )
    op.add_column(
        "export_sap",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("export_sap", sa.Column("remote_call_id", sa.String(length=191), nullable=True))
    op.add_column("export_sap", sa.Column("rma_no", sa.String(length=30), nullable=True))
    op.add_column("export_sap", sa.Column("last_error_code", sa.String(length=100), nullable=True))
    op.add_column("export_sap", sa.Column("last_error_message", sa.Text(), nullable=True))
    op.add_column("export_sap", sa.Column("next_retry_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("export_sap", sa.Column("submitted_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("export_sap", sa.Column("accepted_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("export_sap", sa.Column("last_polled_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("export_sap", sa.Column("rma_received_at", mysql.DATETIME(fsp=3), nullable=True))
    op.alter_column(
        "export_sap",
        "shipping_fee",
        existing_type=sa.Numeric(precision=18, scale=2),
        type_=sa.String(length=100),
        existing_nullable=True,
    )
    op.create_foreign_key(
        "fk_export_sap_ticket",
        "export_sap",
        "repair_tickets",
        ["ticket_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_export_sap_ticket_item",
        "export_sap",
        "repair_ticket_items",
        ["ticket_item_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_export_sap_relay_export",
        "export_sap",
        "ticket_relay_exports",
        ["relay_export_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint("uk_export_sap_submission_key", "export_sap", ["submission_key"])
    op.create_unique_constraint("uk_export_sap_remote_call_id", "export_sap", ["remote_call_id"])
    op.create_unique_constraint(
        "uk_export_sap_item_snapshot",
        "export_sap",
        ["ticket_item_id", "ticket_version", "payload_hash"],
    )
    op.create_index("idx_export_sap_ticket", "export_sap", ["ticket_id", "created_at"], unique=False)
    op.create_index("idx_export_sap_relay", "export_sap", ["relay_export_id", "status"], unique=False)
    op.create_index("idx_export_sap_status_retry", "export_sap", ["status", "next_retry_at"], unique=False)
    op.create_index("idx_export_sap_rma", "export_sap", ["rma_no"], unique=False)

    op.create_table(
        "customer_service_policies",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("policy_code", sa.String(length=100), nullable=False),
        sa.Column("customer_code", sa.String(length=50), nullable=False),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("policy_type", sa.String(length=30), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_until", sa.Date(), nullable=True),
        sa.Column("repair_price", sa.Numeric(precision=18, scale=2), server_default="0", nullable=False),
        sa.Column("currency", sa.String(length=10), server_default="CNY", nullable=False),
        sa.Column("tax_rate", sa.Numeric(precision=8, scale=4), server_default="13", nullable=False),
        sa.Column(
            "shipping_fee_text",
            sa.String(length=100),
            server_default="one-way charge/单次收费",
            nullable=False,
        ),
        sa.Column("reply_salutation", sa.String(length=100), nullable=True),
        sa.Column(
            "hide_company_name",
            mysql.TINYINT(display_width=1),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "force_manual_review",
            mysql.TINYINT(display_width=1),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            mysql.TINYINT(display_width=1),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("source_file_name", sa.String(length=255), nullable=True),
        sa.Column("source_row_no", sa.Integer(), nullable=True),
        sa.Column("imported_by_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("imported_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["imported_by_user_id"],
            ["users.id"],
            name="fk_customer_service_policies_imported_by",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_service_policies")),
        sa.UniqueConstraint("policy_code", name="uk_customer_service_policies_code"),
    )
    op.create_index(
        "idx_customer_service_policies_customer",
        "customer_service_policies",
        ["customer_code", "enabled"],
        unique=False,
    )
    op.create_index(
        "idx_customer_service_policies_type",
        "customer_service_policies",
        ["policy_type", "enabled"],
        unique=False,
    )
    op.create_index(
        "idx_customer_service_policies_effective",
        "customer_service_policies",
        ["effective_from", "effective_until"],
        unique=False,
    )

    policy_table = sa.table(
        "customer_service_policies",
        sa.column("policy_code", sa.String()),
        sa.column("customer_code", sa.String()),
        sa.column("customer_name", sa.String()),
        sa.column("policy_type", sa.String()),
        sa.column("repair_price", sa.Numeric()),
        sa.column("currency", sa.String()),
        sa.column("tax_rate", sa.Numeric()),
        sa.column("shipping_fee_text", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("source_file_name", sa.String()),
        sa.column("source_row_no", sa.Integer()),
    )
    rows = [
        {
            "policy_code": "default-out-of-warranty",
            "customer_code": "*",
            "customer_name": "默认超保政策",
            "policy_type": "default",
            "repair_price": 1200,
            "currency": "CNY",
            "tax_rate": 13,
            "shipping_fee_text": "one-way charge/单次收费",
            "enabled": True,
            "source_file_name": "system-default",
            "source_row_no": 1,
        }
    ]
    rows.extend(
        {
            "policy_code": f"permanent-free-{code}",
            "customer_code": code,
            "customer_name": name,
            "policy_type": "permanent_free",
            "repair_price": 0,
            "currency": "CNY",
            "tax_rate": 13,
            "shipping_fee_text": "one-way charge/单次收费",
            "enabled": True,
            "source_file_name": "包年免费.xls",
            "source_row_no": index,
        }
        for index, (code, name) in enumerate(FREE_POLICIES, start=2)
    )
    rows.extend(
        {
            "policy_code": f"annual-free-{code}",
            "customer_code": code,
            "customer_name": name,
            "policy_type": "annual_free",
            "repair_price": 0,
            "currency": "CNY",
            "tax_rate": 13,
            "shipping_fee_text": "one-way charge/单次收费",
            "enabled": False,
            "source_file_name": "包年免费.xls",
            "source_row_no": index,
        }
        for index, (code, name) in enumerate(ANNUAL_POLICIES, start=22)
    )
    rows.extend(
        {
            "policy_code": f"special-out-of-warranty-{code}",
            "customer_code": code,
            "customer_name": name,
            "policy_type": "special_out_of_warranty",
            "repair_price": price,
            "currency": currency,
            "tax_rate": tax_rate,
            "shipping_fee_text": "one-way charge/单次收费",
            "enabled": True,
            "source_file_name": "unnormal_price.png",
            "source_row_no": index,
        }
        for index, (code, name, price, tax_rate, currency) in enumerate(SPECIAL_POLICIES, start=2)
    )
    op.bulk_insert(policy_table, rows)

    op.create_table(
        "ticket_rmas",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("rma_no", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="received", nullable=False),
        sa.Column("policy_snapshot", mysql.JSON(), nullable=True),
        sa.Column("pdf_oss_object_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("reply_record_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("received_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("sent_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["repair_tickets.id"],
            name="fk_ticket_rmas_ticket",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pdf_oss_object_id"],
            ["oss_objects.id"],
            name="fk_ticket_rmas_pdf",
        ),
        sa.ForeignKeyConstraint(
            ["reply_record_id"],
            ["reply_records.id"],
            name="fk_ticket_rmas_reply",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_rmas")),
        sa.UniqueConstraint("rma_no", name="uk_ticket_rmas_no"),
    )
    op.create_index("idx_ticket_rmas_ticket", "ticket_rmas", ["ticket_id", "created_at"], unique=False)
    op.create_index("idx_ticket_rmas_status", "ticket_rmas", ["status", "updated_at"], unique=False)

    op.create_table(
        "ticket_rma_items",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("ticket_rma_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("ticket_item_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ticket_rma_id"],
            ["ticket_rmas.id"],
            name="fk_ticket_rma_items_rma",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_item_id"],
            ["repair_ticket_items.id"],
            name="fk_ticket_rma_items_item",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_rma_items")),
        sa.UniqueConstraint("ticket_item_id", name="uk_ticket_rma_items_ticket_item"),
    )
    op.create_index("idx_ticket_rma_items_rma", "ticket_rma_items", ["ticket_rma_id"], unique=False)

    op.execute(
        """
        INSERT INTO workflow_statuses
            (status_code, status_name, status_category, description, is_terminal, sort_order, enabled)
        VALUES
            ('rma_sent', 'RMA已发送', 'rma',
             '全部SN已取得同一RMA编号，RMA模板回复已在原邮件链发送成功。',
             0, 70, 1)
        ON DUPLICATE KEY UPDATE
            status_name=VALUES(status_name),
            status_category=VALUES(status_category),
            description=VALUES(description),
            sort_order=VALUES(sort_order),
            enabled=1
        """
    )
    op.execute(
        """
        INSERT INTO workflow_transitions
            (from_status_code, to_status_code, trigger_event, condition_desc, require_manual, enabled)
        VALUES
            ('ready_for_export', 'rma_sent', 'rma_reply_sent',
             'SAP回填合法RMA编号且模板回复实际发送成功。', 0, 1),
            ('rma_sent', 'closed', 'device_receipt_ack_sent',
             '公司收货确认回复实际发送成功后闭单。', 0, 1),
            ('rma_sent', 'manual_review', 'manual_review_required',
             'RMA发送后的异常需要人工处理。', 1, 1),
            ('rma_sent', 'error', 'system_error',
             'RMA发送后的系统异常。', 1, 1)
        ON DUPLICATE KEY UPDATE
            condition_desc=VALUES(condition_desc),
            require_manual=VALUES(require_manual),
            enabled=1
        """
    )
    op.execute(
        "UPDATE workflow_transitions SET enabled=0 "
        "WHERE to_status_code='closed' "
        "AND NOT (from_status_code='rma_sent' AND trigger_event='device_receipt_ack_sent')"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM workflow_transitions "
        "WHERE (from_status_code='ready_for_export' AND to_status_code='rma_sent') "
        "OR from_status_code='rma_sent'"
    )
    op.execute(
        "UPDATE workflow_transitions SET enabled=1 "
        "WHERE from_status_code='ready_for_export' AND to_status_code='closed' "
        "AND trigger_event='device_receipt_ack_sent'"
    )
    op.execute("DELETE FROM workflow_statuses WHERE status_code='rma_sent'")

    op.drop_index("idx_ticket_rma_items_rma", table_name="ticket_rma_items")
    op.drop_table("ticket_rma_items")
    op.drop_index("idx_ticket_rmas_status", table_name="ticket_rmas")
    op.drop_index("idx_ticket_rmas_ticket", table_name="ticket_rmas")
    op.drop_table("ticket_rmas")

    op.drop_index("idx_customer_service_policies_effective", table_name="customer_service_policies")
    op.drop_index("idx_customer_service_policies_type", table_name="customer_service_policies")
    op.drop_index("idx_customer_service_policies_customer", table_name="customer_service_policies")
    op.drop_table("customer_service_policies")

    op.drop_index("idx_export_sap_rma", table_name="export_sap")
    op.drop_index("idx_export_sap_status_retry", table_name="export_sap")
    op.drop_index("idx_export_sap_relay", table_name="export_sap")
    op.drop_index("idx_export_sap_ticket", table_name="export_sap")
    op.drop_constraint("uk_export_sap_item_snapshot", "export_sap", type_="unique")
    op.drop_constraint("uk_export_sap_remote_call_id", "export_sap", type_="unique")
    op.drop_constraint("uk_export_sap_submission_key", "export_sap", type_="unique")
    op.drop_constraint("fk_export_sap_relay_export", "export_sap", type_="foreignkey")
    op.drop_constraint("fk_export_sap_ticket_item", "export_sap", type_="foreignkey")
    op.drop_constraint("fk_export_sap_ticket", "export_sap", type_="foreignkey")
    op.execute("UPDATE export_sap SET shipping_fee=NULL")
    op.alter_column(
        "export_sap",
        "shipping_fee",
        existing_type=sa.String(length=100),
        type_=sa.Numeric(precision=18, scale=2),
        existing_nullable=True,
    )
    for column in (
        "rma_received_at",
        "last_polled_at",
        "accepted_at",
        "submitted_at",
        "next_retry_at",
        "last_error_message",
        "last_error_code",
        "rma_no",
        "remote_call_id",
        "attempt_count",
        "status",
        "payload_hash",
        "policy_snapshot",
        "submission_key",
        "ticket_version",
        "relay_export_id",
        "ticket_item_id",
        "ticket_id",
    ):
        op.drop_column("export_sap", column)

    op.drop_column("repair_tickets", "closed_at")
    op.drop_column("repair_tickets", "terminal_reason")
    op.drop_column("repair_tickets", "terminal_reason_code")
    op.drop_column("emails", "next_retry_at")
    op.drop_column("emails", "retryable")
    op.drop_column("emails", "last_error_code")
    op.drop_column("emails", "terminal_reason_code")
    op.drop_column("emails", "processing_stage")
