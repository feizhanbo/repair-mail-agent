"""use RMB as canonical business currency

Revision ID: t7o2j3k4l5m6
Revises: s6n1i2j3k4l5
"""

from alembic import op
import sqlalchemy as sa


revision = "t7o2j3k4l5m6"
down_revision = "s6n1i2j3k4l5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "customer_service_policies",
        "currency",
        existing_type=sa.String(length=10),
        server_default="RMB",
        existing_nullable=False,
    )
    op.execute("UPDATE customer_service_policies SET currency = 'RMB' WHERE UPPER(currency) = 'CNY'")
    op.execute(
        "UPDATE export_sap SET currency = 'RMB' WHERE UPPER(currency) = 'CNY'"
    )
    op.execute(
        "UPDATE repair_tickets SET policy_snapshot = JSON_SET(policy_snapshot, '$.currency', 'RMB') "
        "WHERE policy_snapshot IS NOT NULL "
        "AND JSON_UNQUOTE(JSON_EXTRACT(policy_snapshot, '$.currency')) = 'CNY'"
    )
    op.execute(
        "UPDATE export_sap SET policy_snapshot = JSON_SET(policy_snapshot, '$.currency', 'RMB') "
        "WHERE policy_snapshot IS NOT NULL "
        "AND JSON_UNQUOTE(JSON_EXTRACT(policy_snapshot, '$.currency')) = 'CNY'"
    )


def downgrade() -> None:
    op.execute("UPDATE customer_service_policies SET currency = 'CNY' WHERE UPPER(currency) = 'RMB'")
    op.execute(
        "UPDATE export_sap SET currency = 'CNY' WHERE UPPER(currency) = 'RMB'"
    )
    op.execute(
        "UPDATE repair_tickets SET policy_snapshot = JSON_SET(policy_snapshot, '$.currency', 'CNY') "
        "WHERE policy_snapshot IS NOT NULL "
        "AND JSON_UNQUOTE(JSON_EXTRACT(policy_snapshot, '$.currency')) = 'RMB'"
    )
    op.alter_column(
        "customer_service_policies",
        "currency",
        existing_type=sa.String(length=10),
        server_default="CNY",
        existing_nullable=False,
    )
