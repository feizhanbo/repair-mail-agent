"""upgrade reply signatures and split domestic RMA warranty templates

Revision ID: s6n1i2j3k4l5
Revises: r5m0h1c2d3e4
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.seed import REPLY_TEMPLATES


revision: str = "s6n1i2j3k4l5"
down_revision: str | None = "r5m0h1c2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ERROR_CODE = "TEMPLATE_CONTENT_UPGRADED_REGENERATE_REQUIRED"
MANAGED_KEYS = {
    ("all_replies_domestic_company_base_zh", "v2"),
    ("all_replies_international_company_base_en", "v1"),
    ("rma_authorization_domestic_in_warranty_zh", "domestic_in_warranty_v1"),
    ("rma_authorization_domestic_out_of_warranty_zh", "domestic_out_warranty_v1"),
}


def _managed_templates() -> list[dict[str, object]]:
    return [item for item in REPLY_TEMPLATES if (str(item["template_code"]), str(item["version"])) in MANAGED_KEYS]


def upgrade() -> None:
    for item in _managed_templates():
        params = {
            "template_code": item["template_code"], "template_name": item["template_name"],
            "template_type": item["template_type"], "language": item["language"],
            "version": item["version"], "subject_template": item.get("subject_template"),
            "body_template": item["body_template"], "html_body_template": item.get("html_body_template"),
            "enabled": bool(item.get("enabled", True)),
        }
        op.execute(sa.text(
            "INSERT INTO reply_templates (template_code,template_name,template_type,language,version,subject_template,body_template,html_body_template,enabled,created_at,updated_at) "
            "VALUES (:template_code,:template_name,:template_type,:language,:version,:subject_template,:body_template,:html_body_template,:enabled,CURRENT_TIMESTAMP(3),CURRENT_TIMESTAMP(3)) "
            "ON DUPLICATE KEY UPDATE template_name=VALUES(template_name),template_type=VALUES(template_type),language=VALUES(language),subject_template=VALUES(subject_template),body_template=VALUES(body_template),html_body_template=VALUES(html_body_template),enabled=VALUES(enabled),updated_at=CURRENT_TIMESTAMP(3)"
        ).bindparams(**params))
    op.execute(sa.text(
        "UPDATE reply_templates SET enabled=0,updated_at=CURRENT_TIMESTAMP(3) WHERE template_code='rma_authorization_domestic_zh'"
    ))
    op.execute(sa.text(
        "UPDATE reply_records SET send_status='send_failed',last_error_code=:error_code,error_message=:error_code,next_retry_at=NULL,updated_at=CURRENT_TIMESTAMP(3) WHERE send_status IN ('pending_review','approved_pending_send')"
    ).bindparams(error_code=ERROR_CODE))


def downgrade() -> None:
    op.execute(sa.text(
        "UPDATE reply_templates SET enabled=1,updated_at=CURRENT_TIMESTAMP(3) WHERE template_code='rma_authorization_domestic_zh'"
    ))
    for code, version in MANAGED_KEYS - {("all_replies_domestic_company_base_zh", "v2")}:
        op.execute(sa.text(
            "UPDATE reply_templates SET enabled=0,updated_at=CURRENT_TIMESTAMP(3) WHERE template_code=:code AND version=:version"
        ).bindparams(code=code, version=version))
    op.execute(sa.text(
        "UPDATE reply_records SET last_error_code='REPLY_REGENERATE_AFTER_DOWNGRADE_REQUIRED',error_message='REPLY_REGENERATE_AFTER_DOWNGRADE_REQUIRED',updated_at=CURRENT_TIMESTAMP(3) WHERE last_error_code=:error_code"
    ).bindparams(error_code=ERROR_CODE))
