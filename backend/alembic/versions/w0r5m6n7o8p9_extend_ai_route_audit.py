"""extend AI route and prompt audit

Revision ID: w0r5m6n7o8p9
Revises: v9q4l5m6n7o8
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "w0r5m6n7o8p9"
down_revision = "v9q4l5m6n7o8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_call_logs", sa.Column("prompt_hash", mysql.CHAR(64)))
    op.add_column("ai_call_logs", sa.Column("route_name", sa.String(100)))
    op.add_column("ai_call_logs", sa.Column("route_attempt", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("ai_call_logs", sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.create_index("idx_ai_logs_route_time", "ai_call_logs", ["route_name", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_ai_logs_route_time", table_name="ai_call_logs")
    op.drop_column("ai_call_logs", "fallback_used")
    op.drop_column("ai_call_logs", "route_attempt")
    op.drop_column("ai_call_logs", "route_name")
    op.drop_column("ai_call_logs", "prompt_hash")
