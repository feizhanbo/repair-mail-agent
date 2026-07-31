"""add explicit email intent subtype

Revision ID: g4b8c9d0e1f2
Revises: f3a7b8c9d0e1
Create Date: 2026-07-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "g4b8c9d0e1f2"
down_revision: str | None = "f3a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("emails", sa.Column("intent_subtype", sa.String(length=50), nullable=True))
    op.create_index("idx_emails_intent_subtype", "emails", ["intent_subtype"])
    op.add_column("parse_results", sa.Column("intent_subtype", sa.String(length=50), nullable=True))
    op.create_index("idx_parse_results_intent_subtype", "parse_results", ["intent_subtype"])
    op.execute(
        "UPDATE emails SET intent_subtype = 'general_irrelevant' "
        "WHERE intent_type = 'irrelevant' AND intent_subtype IS NULL"
    )
    op.execute(
        "UPDATE parse_results SET intent_subtype = 'general_irrelevant' "
        "WHERE intent_type = 'irrelevant' AND intent_subtype IS NULL"
    )


def downgrade() -> None:
    op.drop_index("idx_parse_results_intent_subtype", table_name="parse_results")
    op.drop_column("parse_results", "intent_subtype")
    op.drop_index("idx_emails_intent_subtype", table_name="emails")
    op.drop_column("emails", "intent_subtype")
