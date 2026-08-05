"""add reply HTML templates and immutable render evidence

Revision ID: l9g4b5c6d7e8
Revises: k8f3a4b5c6d7
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "l9g4b5c6d7e8"
down_revision: str | None = "k8f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reply_templates", sa.Column("html_body_template", mysql.MEDIUMTEXT(), nullable=True))
    op.add_column("reply_records", sa.Column("draft_html_body", mysql.MEDIUMTEXT(), nullable=True))
    op.add_column("reply_records", sa.Column("final_html_body", mysql.MEDIUMTEXT(), nullable=True))
    op.add_column("reply_records", sa.Column("thread_history_hash", mysql.CHAR(64), nullable=True))
    op.add_column("reply_records", sa.Column("render_hash", mysql.CHAR(64), nullable=True))


def downgrade() -> None:
    op.drop_column("reply_records", "render_hash")
    op.drop_column("reply_records", "thread_history_hash")
    op.drop_column("reply_records", "final_html_body")
    op.drop_column("reply_records", "draft_html_body")
    op.drop_column("reply_templates", "html_body_template")
