"""add AI schema and parser audit metadata

Revision ID: f9a4b5c6d7e8
Revises: e8z3a4b5c6d7
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "f9a4b5c6d7e8"
down_revision: str | None = "e8z3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_call_logs", sa.Column("schema_version", sa.String(length=80), nullable=True))
    op.add_column("ai_call_logs", sa.Column("parser_version", sa.String(length=80), nullable=True))
    op.add_column("ai_call_logs", sa.Column("structured_output_method", sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_call_logs", "structured_output_method")
    op.drop_column("ai_call_logs", "parser_version")
    op.drop_column("ai_call_logs", "schema_version")
