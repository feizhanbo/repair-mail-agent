"""preserve unknown historical SAP charge status

Revision ID: j7e1f2a3b4c5
Revises: i6d0e1f2a3b4
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op


revision: str = "j7e1f2a3b4c5"
down_revision: str | None = "i6d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE export_sap
        SET charge_status = NULL
        WHERE charge_status = 'manual_confirmation'
          AND (
              JSON_UNQUOTE(JSON_EXTRACT(policy_snapshot, '$.policy_type')) IS NULL
              OR JSON_UNQUOTE(JSON_EXTRACT(policy_snapshot, '$.policy_type'))
                 NOT IN (
                     'permanent_free',
                     'annual_free',
                     'special_out_of_warranty',
                     'warranty'
                 )
          )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE export_sap
        SET charge_status = 'manual_confirmation'
        WHERE charge_status IS NULL
        """
    )
