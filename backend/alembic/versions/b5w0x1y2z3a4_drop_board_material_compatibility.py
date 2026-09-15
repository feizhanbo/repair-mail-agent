"""drop board-card material compatibility columns

Revision ID: b5w0x1y2z3a4
Revises: a4v9w0x1y2z3
Create Date: 2026-09-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b5w0x1y2z3a4"
down_revision: str | None = "a4v9w0x1y2z3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("idx_board_cards_material_name", table_name="board_cards")
    op.drop_column("board_cards", "material_name")
    op.drop_column("board_cards", "material_code")


def downgrade() -> None:
    op.add_column("board_cards", sa.Column("material_code", sa.String(100), nullable=True))
    op.add_column("board_cards", sa.Column("material_name", sa.String(255), nullable=True))
    op.execute(
        "UPDATE board_cards SET material_code = board_code, material_name = board_name"
    )
    op.alter_column(
        "board_cards", "material_code", existing_type=sa.String(100), nullable=False
    )
    op.create_index(
        "idx_board_cards_material_name", "board_cards", ["material_name"]
    )
