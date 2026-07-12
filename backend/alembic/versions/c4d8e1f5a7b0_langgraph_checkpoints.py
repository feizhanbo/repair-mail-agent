"""langgraph checkpoints

Revision ID: c4d8e1f5a7b0
Revises: b3e1f7d2a4c0
Create Date: 2026-07-12 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "c4d8e1f5a7b0"
down_revision = "b3e1f7d2a4c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graph_runs",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("graph_run_id", mysql.CHAR(36), nullable=False),
        sa.Column("graph_name", sa.String(length=100), nullable=False),
        sa.Column("email_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="running", nullable=False),
        sa.Column("started_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("finished_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], name="fk_graph_runs_email"),
        sa.ForeignKeyConstraint(["ticket_id"], ["repair_tickets.id"], name="fk_graph_runs_ticket"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_runs")),
        sa.UniqueConstraint("graph_run_id", name=op.f("uq_graph_runs_graph_run_id")),
    )
    op.create_index("idx_graph_runs_email", "graph_runs", ["email_id"], unique=False)
    op.create_index("idx_graph_runs_ticket", "graph_runs", ["ticket_id"], unique=False)
    op.create_index("idx_graph_runs_status", "graph_runs", ["status", "created_at"], unique=False)

    op.create_table(
        "graph_node_logs",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("graph_run_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("node_name", sa.String(length=100), nullable=False),
        sa.Column("input_summary", sa.Text(), nullable=True),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="running", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("finished_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(["graph_run_id"], ["graph_runs.id"], name="fk_graph_node_logs_run", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_node_logs")),
    )
    op.create_index("idx_graph_node_logs_run", "graph_node_logs", ["graph_run_id"], unique=False)
    op.create_index("idx_graph_node_logs_status", "graph_node_logs", ["status"], unique=False)

    op.create_table(
        "langgraph_checkpoints",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("parent_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("checkpoint", mysql.LONGBLOB(), nullable=True),
        sa.Column("metadata", mysql.LONGBLOB(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id", name=op.f("pk_langgraph_checkpoints")),
    )
    op.create_index("idx_checkpoints_thread", "langgraph_checkpoints", ["thread_id"], unique=False)
    op.create_index("idx_checkpoints_parent", "langgraph_checkpoints", ["parent_checkpoint_id"], unique=False)

    op.create_table(
        "langgraph_checkpoint_writes",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("value", mysql.LONGBLOB(), nullable=True),
        sa.PrimaryKeyConstraint(
            "thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx",
            name=op.f("pk_langgraph_checkpoint_writes"),
        ),
    )
    op.create_index("idx_checkpoint_writes_thread", "langgraph_checkpoint_writes", ["thread_id"], unique=False)
    op.create_index("idx_checkpoint_writes_checkpoint", "langgraph_checkpoint_writes", ["thread_id", "checkpoint_ns", "checkpoint_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_checkpoint_writes_checkpoint", table_name="langgraph_checkpoint_writes")
    op.drop_index("idx_checkpoint_writes_thread", table_name="langgraph_checkpoint_writes")
    op.drop_table("langgraph_checkpoint_writes")

    op.drop_index("idx_checkpoints_parent", table_name="langgraph_checkpoints")
    op.drop_index("idx_checkpoints_thread", table_name="langgraph_checkpoints")
    op.drop_table("langgraph_checkpoints")

    op.drop_index("idx_graph_node_logs_status", table_name="graph_node_logs")
    op.drop_index("idx_graph_node_logs_run", table_name="graph_node_logs")
    op.drop_table("graph_node_logs")

    op.drop_index("idx_graph_runs_status", table_name="graph_runs")
    op.drop_index("idx_graph_runs_ticket", table_name="graph_runs")
    op.drop_index("idx_graph_runs_email", table_name="graph_runs")
    op.drop_table("graph_runs")
