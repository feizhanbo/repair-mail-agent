"""fix reply workflow transitions and duplicate manual tasks

Revision ID: d5e9f7a2b4c6
Revises: c4d8e6f1a2b3
Create Date: 2026-07-15 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "d5e9f7a2b4c6"
down_revision = "c4d8e6f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE workflow_transitions SET enabled = 0 "
        "WHERE from_status_code = 'need_customer_info' AND to_status_code = 'auto_replied' "
        "AND trigger_event = 'reply_draft_created'"
    )
    op.execute(
        "INSERT INTO workflow_transitions "
        "(from_status_code, to_status_code, trigger_event, condition_desc, require_manual, enabled, created_at, updated_at) "
        "VALUES "
        "('need_customer_info', 'auto_replied', 'reply_sent', '补充信息邮件已成功发送。', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
        "('manual_review', 'auto_replied', 'reply_sent', '人工审核后的补充信息邮件已成功发送。', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON DUPLICATE KEY UPDATE condition_desc = VALUES(condition_desc), enabled = 1, updated_at = CURRENT_TIMESTAMP"
    )
    op.execute(
        "UPDATE manual_review_tasks AS generic_task "
        "JOIN manual_review_tasks AS specialized_task ON specialized_task.ticket_id = generic_task.ticket_id "
        "AND specialized_task.task_type IN ('followup_limit', 'reply_review') "
        "AND specialized_task.status IN ('pending', 'claimed', 'assigned') "
        "SET generic_task.status = 'closed', generic_task.resolution = 'Merged into specialized manual task', "
        "generic_task.resolved_at = COALESCE(generic_task.resolved_at, CURRENT_TIMESTAMP), generic_task.updated_at = CURRENT_TIMESTAMP "
        "WHERE generic_task.task_type = 'manual' AND generic_task.status IN ('pending', 'claimed', 'assigned')"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM workflow_transitions WHERE to_status_code = 'auto_replied' "
        "AND trigger_event = 'reply_sent' AND from_status_code IN ('need_customer_info', 'manual_review')"
    )
    op.execute(
        "UPDATE workflow_transitions SET enabled = 1 "
        "WHERE from_status_code = 'need_customer_info' AND to_status_code = 'auto_replied' "
        "AND trigger_event = 'reply_draft_created'"
    )
