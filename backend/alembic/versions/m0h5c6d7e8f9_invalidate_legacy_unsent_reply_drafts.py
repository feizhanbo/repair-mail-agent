"""invalidate legacy unsent reply drafts without render evidence

Revision ID: m0h5c6d7e8f9
Revises: l9g4b5c6d7e8
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "m0h5c6d7e8f9"
down_revision: str | None = "l9g4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ERROR_CODE = "REPLY_REGENERATE_AFTER_TEMPLATE_MIGRATION_REQUIRED"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE reply_records
            SET send_status = 'send_failed',
                last_error_code = :error_code,
                error_message = :error_code,
                next_retry_at = NULL,
                updated_at = CURRENT_TIMESTAMP(3)
            WHERE send_status IN ('pending_review', 'approved_pending_send')
              AND (thread_history_hash IS NULL OR render_hash IS NULL)
            """
        ).bindparams(error_code=ERROR_CODE)
    )


def downgrade() -> None:
    # The old draft's exact review/send state is not safely inferable. Keep it
    # failed rather than risking an automatic resend after downgrade.
    op.execute(
        sa.text(
            """
            UPDATE reply_records
            SET last_error_code = 'REPLY_REGENERATE_AFTER_DOWNGRADE_REQUIRED',
                error_message = 'REPLY_REGENERATE_AFTER_DOWNGRADE_REQUIRED',
                updated_at = CURRENT_TIMESTAMP(3)
            WHERE last_error_code = :error_code
            """
        ).bindparams(error_code=ERROR_CODE)
    )
