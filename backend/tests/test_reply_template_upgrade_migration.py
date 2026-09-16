from __future__ import annotations

import importlib.util
from pathlib import Path


MIGRATION = Path(__file__).parents[1] / "alembic" / "versions" / "s6n1i2j3k4l5_upgrade_reply_signatures_and_rma_routes.py"


def test_reply_template_upgrade_migration_has_safe_draft_invalidation() -> None:
    spec = importlib.util.spec_from_file_location("template_upgrade", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = MIGRATION.read_text(encoding="utf-8")
    assert module.down_revision == "r5m0h1c2d3e4"
    assert "pending_review','approved_pending_send" in source
    assert "send_uncertain" not in source
    assert "TEMPLATE_CONTENT_UPGRADED_REGENERATE_REQUIRED" in source
    assert "rma_authorization_domestic_in_warranty_zh" in source
    assert "rma_authorization_domestic_out_of_warranty_zh" in source
