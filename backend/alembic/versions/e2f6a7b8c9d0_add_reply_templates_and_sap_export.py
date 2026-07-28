"""add reply template audit, SN hierarchy and SAP export staging

Revision ID: e2f6a7b8c9d0
Revises: d1e5f6a7b8c9
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "e2f6a7b8c9d0"
down_revision: str | None = "d1e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sn_assets", sa.Column("service_tracking_card_no", sa.String(length=100), nullable=True))
    op.add_column("sn_assets", sa.Column("parent_sn", sa.String(length=100), nullable=True))
    op.add_column("sn_assets", sa.Column("top_sn", sa.String(length=100), nullable=True))
    op.add_column("sn_assets", sa.Column("parent_material_code", sa.String(length=100), nullable=True))
    op.add_column("sn_assets", sa.Column("top_material_code", sa.String(length=100), nullable=True))
    op.create_index("idx_sn_assets_service_tracking_card", "sn_assets", ["service_tracking_card_no"], unique=False)
    op.create_index("idx_sn_assets_parent_sn", "sn_assets", ["parent_sn"], unique=False)
    op.create_index("idx_sn_assets_top_sn", "sn_assets", ["top_sn"], unique=False)

    op.add_column(
        "reply_records",
        sa.Column("base_template_id", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_reply_records_base_template",
        "reply_records",
        "reply_templates",
        ["base_template_id"],
        ["id"],
    )

    op.create_table(
        "export_sap",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("sn", sa.String(length=100), nullable=False),
        sa.Column("customer_code", sa.String(length=50), nullable=True),
        sa.Column("material_code", sa.String(length=100), nullable=True),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("material_name", sa.String(length=255), nullable=True),
        sa.Column("contact_person", sa.String(length=100), nullable=True),
        sa.Column("contact_phone", sa.String(length=100), nullable=True),
        sa.Column("email_subject", sa.String(length=500), nullable=True),
        sa.Column("problem_description", sa.Text(), nullable=True),
        sa.Column("repair_requested_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("mailing_address", sa.String(length=500), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("shipping_fee", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("repair_fee", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("tax_rate", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_sap")),
    )
    op.create_index("idx_export_sap_sn", "export_sap", ["sn"], unique=False)
    op.create_index("idx_export_sap_customer_code", "export_sap", ["customer_code"], unique=False)
    op.create_index("idx_export_sap_material_code", "export_sap", ["material_code"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_export_sap_material_code", table_name="export_sap")
    op.drop_index("idx_export_sap_customer_code", table_name="export_sap")
    op.drop_index("idx_export_sap_sn", table_name="export_sap")
    op.drop_table("export_sap")

    op.drop_constraint("fk_reply_records_base_template", "reply_records", type_="foreignkey")
    op.drop_column("reply_records", "base_template_id")

    op.drop_index("idx_sn_assets_top_sn", table_name="sn_assets")
    op.drop_index("idx_sn_assets_parent_sn", table_name="sn_assets")
    op.drop_index("idx_sn_assets_service_tracking_card", table_name="sn_assets")
    op.drop_column("sn_assets", "top_material_code")
    op.drop_column("sn_assets", "parent_material_code")
    op.drop_column("sn_assets", "top_sn")
    op.drop_column("sn_assets", "parent_sn")
    op.drop_column("sn_assets", "service_tracking_card_no")
