"""align customer, board, service policy and SAP fields

Revision ID: i6d0e1f2a3b4
Revises: h5c9d0e1f2a3
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "i6d0e1f2a3b4"
down_revision: str | None = "h5c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("board_cards", sa.Column("board_code", sa.String(100), nullable=True))
    op.add_column("board_cards", sa.Column("board_name", sa.String(255), nullable=True))
    op.add_column("board_cards", sa.Column("return_location", sa.String(20), nullable=True))
    op.add_column(
        "board_cards",
        sa.Column("route_type", sa.String(30), server_default="board_rule", nullable=False),
    )
    op.add_column(
        "board_cards",
        sa.Column("customer_scope", sa.String(20), server_default="domestic", nullable=False),
    )
    op.execute(
        """
        UPDATE board_cards
        SET board_code = material_code,
            board_name = material_name,
            return_location = CASE
                WHEN need_ship_to_beijing = 1 THEN 'beijing'
                ELSE 'tianjin'
            END
        """
    )
    op.alter_column("board_cards", "board_code", existing_type=sa.String(100), nullable=False)
    op.alter_column("board_cards", "return_location", existing_type=sa.String(20), nullable=False)
    op.drop_constraint("uk_board_cards_material_code", "board_cards", type_="unique")
    op.create_index("idx_board_cards_board_code", "board_cards", ["board_code"])
    op.create_index("idx_board_cards_board_name", "board_cards", ["board_name"])
    op.create_index(
        "idx_board_cards_route",
        "board_cards",
        ["customer_scope", "route_type", "status"],
    )
    op.create_index(
        "idx_board_cards_location",
        "board_cards",
        ["return_location", "status"],
    )

    op.add_column(
        "customer_service_policies",
        sa.Column(
            "charge_status",
            sa.String(30),
            server_default="manual_confirmation",
            nullable=True,
        ),
    )
    op.add_column("customer_service_policies", sa.Column("customer_scope", sa.String(20), nullable=True))
    op.execute(
        """
        UPDATE customer_service_policies
        SET charge_status = CASE policy_type
            WHEN 'permanent_free' THEN 'free'
            WHEN 'annual_free' THEN 'annual_contract'
            WHEN 'special_out_of_warranty' THEN 'chargeable'
            ELSE 'manual_confirmation'
        END
        """
    )
    op.alter_column(
        "customer_service_policies",
        "charge_status",
        existing_type=sa.String(30),
        nullable=False,
    )

    op.add_column("repair_tickets", sa.Column("customer_scope", sa.String(20), nullable=True))
    op.add_column("repair_tickets", sa.Column("customer_scope_source", sa.String(30), nullable=True))
    op.add_column("repair_tickets", sa.Column("charge_status", sa.String(30), nullable=True))
    op.add_column("repair_tickets", sa.Column("charge_status_source", sa.String(30), nullable=True))
    op.add_column(
        "repair_tickets",
        sa.Column("service_policy_id", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.add_column(
        "repair_tickets",
        sa.Column(
            "policy_resolution_status",
            sa.String(30),
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column("repair_tickets", sa.Column("policy_snapshot", mysql.JSON(), nullable=True))
    op.create_foreign_key(
        "fk_repair_tickets_service_policy",
        "repair_tickets",
        "customer_service_policies",
        ["service_policy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_repair_tickets_policy_status",
        "repair_tickets",
        ["policy_resolution_status", "updated_at"],
    )

    op.add_column("repair_ticket_items", sa.Column("board_code", sa.String(100), nullable=True))
    op.add_column("repair_ticket_items", sa.Column("board_name", sa.String(255), nullable=True))
    op.add_column(
        "repair_ticket_items",
        sa.Column("matched_board_card_id", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.add_column("repair_ticket_items", sa.Column("return_location", sa.String(20), nullable=True))
    op.add_column("repair_ticket_items", sa.Column("return_address", sa.String(500), nullable=True))
    op.add_column("repair_ticket_items", sa.Column("return_contact", sa.String(100), nullable=True))
    op.add_column("repair_ticket_items", sa.Column("return_phone", sa.String(100), nullable=True))
    op.add_column("repair_ticket_items", sa.Column("return_postal_code", sa.String(20), nullable=True))
    op.add_column("repair_ticket_items", sa.Column("return_route_source", sa.String(30), nullable=True))
    op.add_column(
        "repair_ticket_items",
        sa.Column(
            "return_route_status",
            sa.String(30),
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column("repair_ticket_items", sa.Column("return_route_message", sa.Text(), nullable=True))
    op.add_column("repair_ticket_items", sa.Column("return_route_snapshot", mysql.JSON(), nullable=True))
    op.create_foreign_key(
        "fk_ticket_items_board_card",
        "repair_ticket_items",
        "board_cards",
        ["matched_board_card_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_ticket_items_board_code", "repair_ticket_items", ["board_code"])
    op.create_index(
        "idx_ticket_items_return_route",
        "repair_ticket_items",
        ["return_route_status", "return_location"],
    )

    op.add_column("export_sap", sa.Column("charge_status", sa.String(30), nullable=True))
    op.execute(
        """
        UPDATE export_sap
        SET charge_status = CASE JSON_UNQUOTE(JSON_EXTRACT(policy_snapshot, '$.policy_type'))
            WHEN 'permanent_free' THEN 'free'
            WHEN 'annual_free' THEN 'annual_contract'
            WHEN 'special_out_of_warranty' THEN 'chargeable'
            WHEN 'warranty' THEN 'free'
            ELSE 'manual_confirmation'
        END
        WHERE charge_status IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("export_sap", "charge_status")

    op.drop_index("idx_ticket_items_return_route", table_name="repair_ticket_items")
    op.drop_index("idx_ticket_items_board_code", table_name="repair_ticket_items")
    op.drop_constraint("fk_ticket_items_board_card", "repair_ticket_items", type_="foreignkey")
    op.drop_column("repair_ticket_items", "return_route_snapshot")
    op.drop_column("repair_ticket_items", "return_route_message")
    op.drop_column("repair_ticket_items", "return_route_status")
    op.drop_column("repair_ticket_items", "return_route_source")
    op.drop_column("repair_ticket_items", "return_postal_code")
    op.drop_column("repair_ticket_items", "return_phone")
    op.drop_column("repair_ticket_items", "return_contact")
    op.drop_column("repair_ticket_items", "return_address")
    op.drop_column("repair_ticket_items", "return_location")
    op.drop_column("repair_ticket_items", "matched_board_card_id")
    op.drop_column("repair_ticket_items", "board_name")
    op.drop_column("repair_ticket_items", "board_code")

    op.drop_index("idx_repair_tickets_policy_status", table_name="repair_tickets")
    op.drop_constraint("fk_repair_tickets_service_policy", "repair_tickets", type_="foreignkey")
    op.drop_column("repair_tickets", "policy_snapshot")
    op.drop_column("repair_tickets", "policy_resolution_status")
    op.drop_column("repair_tickets", "service_policy_id")
    op.drop_column("repair_tickets", "charge_status_source")
    op.drop_column("repair_tickets", "charge_status")
    op.drop_column("repair_tickets", "customer_scope_source")
    op.drop_column("repair_tickets", "customer_scope")

    op.drop_column("customer_service_policies", "customer_scope")
    op.drop_column("customer_service_policies", "charge_status")

    op.drop_index("idx_board_cards_location", table_name="board_cards")
    op.drop_index("idx_board_cards_route", table_name="board_cards")
    op.drop_index("idx_board_cards_board_name", table_name="board_cards")
    op.drop_index("idx_board_cards_board_code", table_name="board_cards")
    op.create_unique_constraint(
        "uk_board_cards_material_code",
        "board_cards",
        ["material_code"],
    )
    op.drop_column("board_cards", "customer_scope")
    op.drop_column("board_cards", "route_type")
    op.drop_column("board_cards", "return_location")
    op.drop_column("board_cards", "board_name")
    op.drop_column("board_cards", "board_code")
