"""langgraph model sync

Revision ID: d5e2f8a6b1c0
Revises: c4d8e1f5a7b0
Create Date: 2026-07-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "d5e2f8a6b1c0"
down_revision = "c4d8e1f5a7b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("graph_runs", sa.Column("graph_thread_id", mysql.CHAR(36), nullable=True))
    op.add_column("graph_runs", sa.Column("current_node", sa.String(100), nullable=True))
    op.add_column("graph_runs", sa.Column("trigger_source", sa.String(50), nullable=False, server_default="api"))
    op.add_column("graph_runs", sa.Column("interrupt_type", sa.String(50), nullable=True))
    op.add_column("graph_runs", sa.Column("interrupt_payload", mysql.JSON, nullable=True))
    op.add_column("graph_runs", sa.Column("manual_task_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("graph_runs", sa.Column("reply_record_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("graph_runs", sa.Column("resume_count", sa.Integer, nullable=False, server_default="0"))
    op.add_column("graph_runs", sa.Column("interrupted_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("graph_runs", sa.Column("resumed_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("graph_runs", sa.Column("expired_at", mysql.DATETIME(fsp=3), nullable=True))
    op.add_column("graph_runs", sa.Column("metadata", mysql.JSON, nullable=True))
    op.create_index("idx_graph_runs_thread_id", "graph_runs", ["graph_thread_id"])
    op.create_index("idx_graph_runs_current_node", "graph_runs", ["current_node"])
    op.create_foreign_key("fk_graph_runs_manual_task", "graph_runs", "manual_review_tasks", ["manual_task_id"], ["id"])
    op.create_foreign_key("fk_graph_runs_reply", "graph_runs", "reply_records", ["reply_record_id"], ["id"])

    op.add_column("graph_node_logs", sa.Column("node_type", sa.String(50), nullable=False, server_default="service"))
    op.add_column("graph_node_logs", sa.Column("llm_call_log_id", mysql.BIGINT(unsigned=True), nullable=True))
    op.add_column("graph_node_logs", sa.Column("token_usage", mysql.JSON, nullable=True))
    op.add_column("graph_node_logs", sa.Column("cost_estimate", mysql.DECIMAL(10, 6), nullable=True))
    op.add_column("graph_node_logs", sa.Column("error_code", sa.String(50), nullable=True))
    op.create_index("idx_graph_node_logs_node_type", "graph_node_logs", ["node_type"])
    op.create_foreign_key("fk_graph_node_logs_ai_log", "graph_node_logs", "ai_call_logs", ["llm_call_log_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_graph_node_logs_ai_log", "graph_node_logs", type_="foreignkey")
    op.drop_index("idx_graph_node_logs_node_type", table_name="graph_node_logs")
    op.drop_column("graph_node_logs", "error_code")
    op.drop_column("graph_node_logs", "cost_estimate")
    op.drop_column("graph_node_logs", "token_usage")
    op.drop_column("graph_node_logs", "llm_call_log_id")
    op.drop_column("graph_node_logs", "node_type")

    op.drop_constraint("fk_graph_runs_reply", "graph_runs", type_="foreignkey")
    op.drop_constraint("fk_graph_runs_manual_task", "graph_runs", type_="foreignkey")
    op.drop_index("idx_graph_runs_current_node", table_name="graph_runs")
    op.drop_index("idx_graph_runs_thread_id", table_name="graph_runs")
    op.drop_column("graph_runs", "metadata")
    op.drop_column("graph_runs", "expired_at")
    op.drop_column("graph_runs", "resumed_at")
    op.drop_column("graph_runs", "interrupted_at")
    op.drop_column("graph_runs", "resume_count")
    op.drop_column("graph_runs", "reply_record_id")
    op.drop_column("graph_runs", "manual_task_id")
    op.drop_column("graph_runs", "interrupt_payload")
    op.drop_column("graph_runs", "interrupt_type")
    op.drop_column("graph_runs", "trigger_source")
    op.drop_column("graph_runs", "current_node")
    op.drop_column("graph_runs", "graph_thread_id")
