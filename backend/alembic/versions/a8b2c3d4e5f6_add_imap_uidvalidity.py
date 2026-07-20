"""scope IMAP UID deduplication by UIDVALIDITY

Revision ID: a8b2c3d4e5f6
Revises: f7a1b2c3d4e5
Create Date: 2026-07-17 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision = "a8b2c3d4e5f6"
down_revision = "f7a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Zero is the compatibility partition for records created before the
    # application began persisting the server's UIDVALIDITY value.
    op.add_column(
        "mail_fetch_records",
        sa.Column("uid_validity", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
    )
    op.drop_constraint("uk_mail_fetch_records", "mail_fetch_records", type_="unique")
    op.create_unique_constraint(
        "uk_mail_fetch_records",
        "mail_fetch_records",
        ["mailbox_account", "folder_name", "uid_validity", "imap_uid"],
    )


def downgrade() -> None:
    # Downgrade can fail if the same numeric UID was legitimately observed in
    # multiple UIDVALIDITY epochs. Operators must deduplicate such rows first.
    op.drop_constraint("uk_mail_fetch_records", "mail_fetch_records", type_="unique")
    op.create_unique_constraint(
        "uk_mail_fetch_records",
        "mail_fetch_records",
        ["mailbox_account", "folder_name", "imap_uid"],
    )
    op.drop_column("mail_fetch_records", "uid_validity")
