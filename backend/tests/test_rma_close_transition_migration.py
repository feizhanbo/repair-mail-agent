from __future__ import annotations

import importlib.util
from pathlib import Path

from app import seed as seed_data


MIGRATION = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "x1s6n7o8p9q0_enable_rma_archive_close_transition.py"
)


def test_rma_close_transition_migration_enables_only_evidence_gated_route() -> None:
    spec = importlib.util.spec_from_file_location("rma_close_transition", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = MIGRATION.read_text(encoding="utf-8")
    assert module.down_revision == "w0r5m6n7o8p9"
    assert "from_status_code='rma_sent'" in source
    assert "to_status_code='closed'" in source
    assert "trigger_event='rma_issued_and_archived'" in source


def test_seed_does_not_disable_all_closed_transitions_after_upsert() -> None:
    source = Path(seed_data.__file__).read_text(encoding="utf-8")
    assert '.where(WorkflowTransition.to_status_code == "closed")' not in source
