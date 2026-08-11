"""complete legacy classification handling levels

Revision ID: p3k8f9a0b1c2
Revises: o2j7e8f9a0b1
Create Date: 2026-08-11
"""

from collections.abc import Sequence

from alembic import op


revision: str = "p3k8f9a0b1c2"
down_revision: str | None = "o2j7e8f9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # UNKNOWN means the historical record cannot be reliably placed in the new
    # taxonomy. Preserve the legacy intent for audit; do not invent a new type.
    op.execute(
        "UPDATE emails SET handling_level='unknown', "
        "classification_version=COALESCE(classification_version,'legacy'), "
        "classification_reason_code='LEGACY_RECLASSIFICATION_REQUIRED' "
        "WHERE handling_level IS NULL"
    )
    op.execute(
        "UPDATE parse_results SET handling_level='unknown', "
        "classification_version=COALESCE(classification_version,'legacy'), "
        "classification_reason_code='LEGACY_RECLASSIFICATION_REQUIRED', "
        "classification_confidence=COALESCE(classification_confidence,confidence_score) "
        "WHERE handling_level IS NULL"
    )


def downgrade() -> None:
    # Mapping the rows back to NULL would erase an explicit audit classification.
    pass
