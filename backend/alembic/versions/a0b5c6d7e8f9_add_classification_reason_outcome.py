"""separate model classification reason from backend outcome

Revision ID: a0b5c6d7e8f9
Revises: f9a4b5c6d7e8
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "a0b5c6d7e8f9"
down_revision: str | None = "f9a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table_name in ("emails", "mail_fetch_records", "parse_results"):
        op.add_column(table_name, sa.Column("classification_model_reason_code", sa.String(length=100), nullable=True))
        op.add_column(table_name, sa.Column("classification_outcome_code", sa.String(length=100), nullable=True))


def downgrade() -> None:
    for table_name in ("parse_results", "mail_fetch_records", "emails"):
        op.drop_column(table_name, "classification_outcome_code")
        op.drop_column(table_name, "classification_model_reason_code")
