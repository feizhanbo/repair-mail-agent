"""add workflow execution and interrupt tracking

Revision ID: r5m0h1c2d3e4
Revises: q4l9g0b1c2d3
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "r5m0h1c2d3e4"
down_revision: str | None = "q4l9g0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_executions",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("execution_id", sa.String(length=64), nullable=False),
        sa.Column("graph_thread_id", sa.String(length=191), nullable=False),
        sa.Column("workflow_name", sa.String(length=100), nullable=False),
        sa.Column("workflow_version", sa.String(length=50), nullable=False),
        sa.Column("state_schema_version", sa.String(length=50), nullable=False),
        sa.Column("execution_mode", sa.String(length=20), server_default="shadow", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="queued", nullable=False),
        sa.Column("email_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("trigger_job_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("current_node", sa.String(length=100), nullable=True),
        sa.Column("last_route", sa.String(length=100), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("checkpoint_id", sa.String(length=100), nullable=True),
        sa.Column("checkpoint_step", sa.Integer(), nullable=True),
        sa.Column("input_fingerprint", mysql.CHAR(length=64), nullable=True),
        sa.Column("result_summary", mysql.JSON(), nullable=True),
        sa.Column("started_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("completed_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], name="fk_workflow_executions_email", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["ticket_id"], ["repair_tickets.id"], name="fk_workflow_executions_ticket", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trigger_job_id"], ["job_run_logs.id"], name="fk_workflow_executions_job", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_executions"),
        sa.UniqueConstraint("execution_id", name="uk_workflow_executions_execution"),
        sa.UniqueConstraint("graph_thread_id", name="uk_workflow_executions_graph_thread"),
    )
    op.create_index("idx_workflow_executions_email", "workflow_executions", ["email_id", "created_at"])
    op.create_index("idx_workflow_executions_ticket", "workflow_executions", ["ticket_id", "created_at"])
    op.create_index("idx_workflow_executions_status", "workflow_executions", ["status", "updated_at"])

    op.create_table(
        "workflow_interrupts",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("execution_id", sa.String(length=64), nullable=False),
        sa.Column("interrupt_id", sa.String(length=100), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=100), nullable=True),
        sa.Column("checkpoint_step", sa.Integer(), nullable=True),
        sa.Column("manual_task_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("request_payload", mysql.JSON(), nullable=False),
        sa.Column("response_payload", mysql.JSON(), nullable=True),
        sa.Column("expected_ticket_version", sa.Integer(), nullable=True),
        sa.Column("resumed_by_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("resumed_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), server_default=sa.text("CURRENT_TIMESTAMP(3)"), nullable=False),
        sa.ForeignKeyConstraint(["manual_task_id"], ["manual_review_tasks.id"], name="fk_workflow_interrupts_manual_task", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_id"], ["workflow_executions.execution_id"], name="fk_workflow_interrupts_execution", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resumed_by_user_id"], ["users.id"], name="fk_workflow_interrupts_resumed_by", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_interrupts"),
        sa.UniqueConstraint("execution_id", "interrupt_id", name="uk_workflow_interrupts_execution_interrupt"),
    )
    op.create_index("idx_workflow_interrupts_task", "workflow_interrupts", ["manual_task_id"])
    op.create_index("idx_workflow_interrupts_status", "workflow_interrupts", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_index("idx_workflow_interrupts_status", table_name="workflow_interrupts")
    op.drop_index("idx_workflow_interrupts_task", table_name="workflow_interrupts")
    op.drop_table("workflow_interrupts")
    op.drop_index("idx_workflow_executions_status", table_name="workflow_executions")
    op.drop_index("idx_workflow_executions_ticket", table_name="workflow_executions")
    op.drop_index("idx_workflow_executions_email", table_name="workflow_executions")
    op.drop_table("workflow_executions")
