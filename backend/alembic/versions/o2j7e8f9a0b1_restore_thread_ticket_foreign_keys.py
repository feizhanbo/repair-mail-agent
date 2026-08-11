"""restore thread and ticket association foreign keys

Revision ID: o2j7e8f9a0b1
Revises: n1i6d7e8f9a0
Create Date: 2026-08-11
"""

from collections.abc import Sequence

from alembic import op


revision: str = "o2j7e8f9a0b1"
down_revision: str | None = "n1i6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # These constraints were declared in the initial migration and ORM but are
    # absent in the deployed MySQL schema. Preflight verifies zero orphan rows.
    op.create_foreign_key(
        "fk_email_threads_latest_email", "email_threads", "emails", ["latest_email_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_email_threads_ticket", "email_threads", "repair_tickets", ["ticket_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_repair_tickets_source_email", "repair_tickets", "emails", ["source_email_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_repair_tickets_thread", "repair_tickets", "email_threads", ["thread_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_repair_tickets_thread", "repair_tickets", type_="foreignkey")
    op.drop_constraint("fk_repair_tickets_source_email", "repair_tickets", type_="foreignkey")
    op.drop_constraint("fk_email_threads_ticket", "email_threads", type_="foreignkey")
    op.drop_constraint("fk_email_threads_latest_email", "email_threads", type_="foreignkey")
