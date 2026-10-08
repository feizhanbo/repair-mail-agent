"""add system reparse recovery transition

Revision ID: e8z3a4b5c6d7
Revises: d7y2z3a4b5c6
Create Date: 2026-09-21
"""

from collections.abc import Sequence

from alembic import op


revision: str = "e8z3a4b5c6d7"
down_revision: str | None = "d7y2z3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO workflow_transitions "
        "(from_status_code, to_status_code, trigger_event, condition_desc, "
        "require_manual, enabled, created_at, updated_at) "
        "SELECT 'manual_review', 'parsed', 'reparse_validation_passed', "
        "'重解析已消除低置信或分类不一致，恢复完整技术校验。', "
        "0, 1, CURRENT_TIMESTAMP(3), CURRENT_TIMESTAMP(3) "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM workflow_transitions "
        "WHERE from_status_code='manual_review' AND to_status_code='parsed' "
        "AND trigger_event='reparse_validation_passed'"
        ")"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM workflow_transitions "
        "WHERE from_status_code='manual_review' AND to_status_code='parsed' "
        "AND trigger_event='reparse_validation_passed'"
    )
