from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models import ReplyRecord, ReplyTemplate


MIGRATION_PATH = Path(__file__).parents[1] / "alembic" / "versions" / "l9g4b5c6d7e8_add_reply_html_and_render_evidence.py"
INVALIDATION_PATH = Path(__file__).parents[1] / "alembic" / "versions" / "m0h5c6d7e8f9_invalidate_legacy_unsent_reply_drafts.py"


def test_reply_html_orm_columns_match_migration_contract() -> None:
    assert {"html_body_template"} <= set(ReplyTemplate.__table__.columns.keys())
    assert {
        "draft_html_body",
        "final_html_body",
        "thread_history_hash",
        "render_hash",
    } <= set(ReplyRecord.__table__.columns.keys())


def test_reply_html_migration_upgrade_and_downgrade_are_symmetric(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("reply_html_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    added: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []

    monkeypatch.setattr(migration.op, "add_column", lambda table, column: added.append((table, column.name)))
    monkeypatch.setattr(migration.op, "drop_column", lambda table, column: dropped.append((table, column)))

    migration.upgrade()
    migration.downgrade()

    assert added == [
        ("reply_templates", "html_body_template"),
        ("reply_records", "draft_html_body"),
        ("reply_records", "final_html_body"),
        ("reply_records", "thread_history_hash"),
        ("reply_records", "render_hash"),
    ]
    assert set(dropped) == set(added)


def test_legacy_draft_invalidation_never_touches_sent_or_uncertain_replies(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("legacy_reply_invalidation", INVALIDATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda statement: statements.append(str(statement)))

    migration.upgrade()

    assert len(statements) == 1
    sql = " ".join(statements[0].split())
    assert "send_status IN ('pending_review', 'approved_pending_send')" in sql
    assert "send_status = 'send_failed'" in sql
    assert "thread_history_hash IS NULL OR render_hash IS NULL" in sql
    assert "send_uncertain" not in sql
    assert "send_status = 'sent'" not in sql
