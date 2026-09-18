"""move runtime configuration to seeded database state and seed scope owners

Revision ID: c6x1y2z3a4b5
Revises: b5w0x1y2z3a4
Create Date: 2026-09-18
"""

from collections.abc import Sequence
import json

from alembic import op
import sqlalchemy as sa

from app.config import settings
from app.core.security import hash_password
from app.seed import OPERATOR_SEEDS, SYSTEM_CONFIG_SEEDS


revision: str = "c6x1y2z3a4b5"
down_revision: str | None = "b5w0x1y2z3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for values in SYSTEM_CONFIG_SEEDS:
        op.execute(sa.text(
            "INSERT IGNORE INTO system_configs "
            "(config_key,config_group,value_type,config_value,version,created_at,updated_at) "
            "VALUES (:config_key,:config_group,:value_type,CAST(:config_value AS JSON),1,CURRENT_TIMESTAMP(3),CURRENT_TIMESTAMP(3))"
        ).bindparams(
            config_key=values["config_key"],
            config_group=values["config_group"],
            value_type=values["value_type"],
            config_value=json.dumps(values["config_value"], ensure_ascii=False),
        ))

    op.execute(sa.text(
        "UPDATE system_configs SET config_value=CAST('true' AS JSON), value_type='boolean', "
        "version=version+1, updated_at=CURRENT_TIMESTAMP(3) "
        "WHERE config_key IN ('auto_send_enabled','auto_followup_enabled')"
    ))
    op.execute(sa.text("DELETE FROM system_configs WHERE config_key='rma_auto_send_enabled'"))

    # Domestic default policies are fully priced and may proceed automatically.
    # Overseas defaults remain human-confirmed by design, but must still carry
    # complete monetary fields so that policy snapshots are valid and editable.
    op.execute(sa.text(
        "UPDATE customer_service_policies SET customer_scope='domestic',updated_at=CURRENT_TIMESTAMP(3) "
        "WHERE policy_type='default' AND customer_code='*' AND customer_scope IS NULL"
    ))
    op.execute(sa.text(
        "UPDATE customer_service_policies SET charge_status='chargeable',repair_price=1200.00,"
        "currency='RMB',tax_rate=13.0000,updated_at=CURRENT_TIMESTAMP(3) "
        "WHERE policy_type='default' AND customer_scope='domestic'"
    ))
    op.execute(sa.text(
        "UPDATE customer_service_policies SET charge_status='manual_confirmation',repair_price=1200.00,"
        "currency='USD',tax_rate=13.0000,updated_at=CURRENT_TIMESTAMP(3) "
        "WHERE policy_type='default' AND customer_scope='overseas'"
    ))

    op.execute(sa.text(
        "INSERT IGNORE INTO roles (role_code,role_name,description,created_at,updated_at) "
        "VALUES ('operator','操作员','处理维修工单、人工复核与业务回复。',CURRENT_TIMESTAMP(3),CURRENT_TIMESTAMP(3))"
    ))

    for values in OPERATOR_SEEDS:
        params = {**values, "password_hash": hash_password(settings.DEFAULT_OPERATOR_PASSWORD)}
        op.execute(sa.text(
            "UPDATE users SET username=:username,real_name=:real_name,email=:email,department=:department,"
            "updated_at=CURRENT_TIMESTAMP(3) WHERE username=:username OR email=:email"
        ).bindparams(**values))
        op.execute(sa.text(
            "INSERT IGNORE INTO users (username,password_hash,real_name,email,department,status,created_at,updated_at) "
            "VALUES (:username,:password_hash,:real_name,:email,:department,'active',CURRENT_TIMESTAMP(3),CURRENT_TIMESTAMP(3))"
        ).bindparams(**params))
        op.execute(sa.text(
            "INSERT IGNORE INTO user_roles (user_id,role_id,created_at) "
            "SELECT u.id,r.id,CURRENT_TIMESTAMP(3) FROM users u JOIN roles r ON r.role_code='operator' "
            "WHERE u.username=:username"
        ).bindparams(username=values["username"]))

    # Historical rows are corrected only when no explicit human owner decision exists.
    op.execute(sa.text(
        "UPDATE repair_tickets rt "
        "JOIN users u ON u.username=CASE rt.customer_scope WHEN 'domestic' THEN 'miya' WHEN 'overseas' THEN 'demi' END "
        "SET rt.assigned_user_id=u.id,rt.updated_at=CURRENT_TIMESTAMP(3) "
        "WHERE rt.customer_scope IN ('domestic','overseas') AND u.status='active' "
        "AND NOT EXISTS (SELECT 1 FROM operation_logs ol WHERE ol.target_type='repair_ticket' "
        "AND ol.target_id=rt.id AND ol.operation_type='ticket_owner_corrected')"
    ))
    op.execute(sa.text(
        "UPDATE manual_review_tasks mt "
        "JOIN repair_tickets rt ON rt.id=mt.ticket_id "
        "JOIN users u ON u.username=CASE rt.customer_scope WHEN 'domestic' THEN 'miya' WHEN 'overseas' THEN 'demi' END "
        "SET mt.assigned_user_id=u.id,mt.status='pending',mt.updated_at=CURRENT_TIMESTAMP(3) "
        "WHERE rt.customer_scope IN ('domestic','overseas') AND u.status='active' "
        "AND mt.status IN ('pending','assigned','assignment_failed') AND mt.claimed_by_user_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM operation_logs ol WHERE ol.target_type='manual_review_task' "
        "AND ol.target_id=mt.id AND ol.operation_type='manual_task_assigned')"
    ))
    op.execute(sa.text(
        "UPDATE reply_templates SET enabled=0,updated_at=CURRENT_TIMESTAMP(3) "
        "WHERE template_code IN ('rma_attachment_disabled_receipt_zh','rma_attachment_disabled_receipt_en')"
    ))


def downgrade() -> None:
    # Forward-only data normalization: user accounts, assignments and runtime values
    # are intentionally retained to avoid deleting or silently reassigning live data.
    pass
