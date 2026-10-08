"""require authoritative repair ticket request date

Revision ID: b1c6d7e8f9a0
Revises: a0b5c6d7e8f9
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql


revision: str = "b1c6d7e8f9a0"
down_revision: str | None = "a0b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE repair_tickets AS ticket
            INNER JOIN emails AS source_email ON source_email.id = ticket.source_email_id
            SET ticket.request_date = DATE(COALESCE(source_email.sent_at, source_email.received_at))
            WHERE ticket.request_date IS NULL
              AND COALESCE(source_email.sent_at, source_email.received_at) IS NOT NULL
            """
        )
    )
    if not context.is_offline_mode():
        unresolved = list(
            connection.execute(
                sa.text(
                    "SELECT id FROM repair_tickets WHERE request_date IS NULL ORDER BY id LIMIT 100"
                )
            ).scalars()
        )
        if unresolved:
            joined_ids = ",".join(str(ticket_id) for ticket_id in unresolved)
            raise RuntimeError(f"REQUEST_DATE_BACKFILL_REQUIRED:{joined_ids}")
    op.alter_column(
        "repair_tickets",
        "request_date",
        existing_type=mysql.DATE(),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "repair_tickets",
        "request_date",
        existing_type=mysql.DATE(),
        nullable=True,
    )
