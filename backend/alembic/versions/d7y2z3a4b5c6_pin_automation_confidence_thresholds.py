"""pin automation confidence thresholds at the approved values

Revision ID: d7y2z3a4b5c6
Revises: c6x1y2z3a4b5
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d7y2z3a4b5c6"
down_revision: str | None = "c6x1y2z3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text(
        "UPDATE system_configs SET config_value=CAST('0.85' AS JSON), value_type='number', "
        "version=version+1, updated_at=CURRENT_TIMESTAMP(3) "
        "WHERE config_key IN ('auto_apply_min_confidence','auto_send_min_confidence')"
    ))


def downgrade() -> None:
    # Runtime safety values are intentionally retained on downgrade.
    pass
