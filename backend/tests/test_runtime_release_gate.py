from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.release_evidence import verify_runtime_release_gate
from app import main


def _settings(tmp_path: Path, *, engine: str = "langgraph", commit: str = "abc123") -> SimpleNamespace:
    return SimpleNamespace(
        WORKFLOW_ENGINE=engine,
        LANGGRAPH_RELEASE_EVIDENCE_FILE=str(tmp_path / "release.json"),
        LANGGRAPH_RELEASE_EVIDENCE_ROOT=str(tmp_path),
        LANGGRAPH_RELEASE_EVIDENCE_MAX_AGE_HOURS=168,
        APP_RELEASE_COMMIT=commit,
    )


def _write_valid_evidence(tmp_path: Path, *, commit: str = "abc123") -> Path:
    path = tmp_path / "release.json"
    report = {
        "schema_version": 2,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "source": {"commit": commit, "dirty": False},
        "requested_probes": {
            "local_test_checkpoint": True,
            "local_test_job_lease": True,
            "local_test_email_dispatch": True,
        },
        "audit_passed": True,
        "requested_checks_passed": True,
        "local_graph_release_gate_passed": True,
        "production_signoff_complete": False,
    }
    payload = (json.dumps(report, indent=2) + "\n").encode("utf-8")
    path.write_bytes(payload)
    path.with_name("release.json.sha256").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  release.json\n",
        encoding="ascii",
    )
    return path


@pytest.mark.parametrize("engine", ["legacy", "shadow"])
def test_non_active_graph_runtime_does_not_require_release_evidence(tmp_path: Path, engine: str) -> None:
    assert verify_runtime_release_gate(_settings(tmp_path, engine=engine, commit="")) is None


@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("LANGGRAPH_RELEASE_EVIDENCE_FILE", "LANGGRAPH_RELEASE_EVIDENCE_FILE_REQUIRED"),
        ("APP_RELEASE_COMMIT", "APP_RELEASE_COMMIT_REQUIRED"),
        ("LANGGRAPH_RELEASE_EVIDENCE_ROOT", "LANGGRAPH_RELEASE_EVIDENCE_ROOT_REQUIRED"),
    ],
)
def test_active_graph_runtime_requires_complete_gate_configuration(
    tmp_path: Path,
    field: str,
    error: str,
) -> None:
    runtime_settings = _settings(tmp_path)
    setattr(runtime_settings, field, "")

    with pytest.raises(RuntimeError, match=error):
        verify_runtime_release_gate(runtime_settings)


def test_active_graph_runtime_verifies_bound_release_evidence(tmp_path: Path) -> None:
    _write_valid_evidence(tmp_path)

    result = verify_runtime_release_gate(_settings(tmp_path))

    assert result is not None
    assert result["verified"] is True
    assert result["source_commit"] == "abc123"


def test_active_graph_runtime_rejects_wrong_commit(tmp_path: Path) -> None:
    _write_valid_evidence(tmp_path, commit="old")

    with pytest.raises(RuntimeError, match="COMMIT_MISMATCH"):
        verify_runtime_release_gate(_settings(tmp_path, commit="new"))


def test_runtime_gate_precedes_runtime_config_rma_and_scheduler_initialization() -> None:
    main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    source = main_path.read_text(encoding="utf-8")
    lifespan_start = source.index("async def lifespan")
    gate = source.index("verify_runtime_release_gate(settings)", lifespan_start)
    runtime_config = source.index("read_runtime_config()", lifespan_start)
    rma_health = source.index("validate_rma_runtime_health()", lifespan_start)
    scheduler = source.index("scheduler = AsyncIOScheduler()", lifespan_start)

    assert gate < runtime_config < rma_health < scheduler


def test_runtime_status_reads_startup_gate_snapshot_instead_of_revalidating() -> None:
    system_path = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "system.py"
    source = system_path.read_text(encoding="utf-8")

    assert '"langgraph_release_gate": getattr(' in source
    assert "request.app.state" in source
    assert "verify_local_release_evidence" not in source


@pytest.mark.anyio
async def test_lifespan_gate_failure_prevents_all_downstream_startup(monkeypatch) -> None:
    runtime_config = Mock()
    rma_health = Mock()
    scheduler = Mock()
    monkeypatch.setattr(
        main,
        "verify_runtime_release_gate",
        Mock(side_effect=RuntimeError("LANGGRAPH_RELEASE_EVIDENCE_GATE_FAILED")),
    )
    monkeypatch.setattr(main, "read_runtime_config", runtime_config)
    monkeypatch.setattr(main, "validate_rma_runtime_health", rma_health)
    monkeypatch.setattr(main, "AsyncIOScheduler", scheduler)

    with pytest.raises(RuntimeError, match="LANGGRAPH_RELEASE_EVIDENCE_GATE_FAILED"):
        async with main.lifespan(main.app):
            pytest.fail("lifespan must not yield after a release gate failure")

    runtime_config.assert_not_called()
    rma_health.assert_not_called()
    scheduler.assert_not_called()


@pytest.mark.anyio
async def test_lifespan_non_graph_mode_records_unrequired_gate_and_starts_scheduler(monkeypatch) -> None:
    scheduler = Mock()
    scheduler_instance = scheduler.return_value
    monkeypatch.setattr(main, "verify_runtime_release_gate", Mock(return_value=None))
    monkeypatch.setattr(main, "read_runtime_config", Mock())
    monkeypatch.setattr(
        main,
        "validate_rma_runtime_health",
        Mock(return_value={"template_version": "test", "template_sha256": "0" * 64, "cjk_font": "test"}),
    )
    monkeypatch.setattr(main, "AsyncIOScheduler", scheduler)

    async with main.lifespan(main.app):
        assert main.app.state.langgraph_release_gate["required"] is False
        assert main.app.state.langgraph_release_gate["verified"] is False

    scheduler_instance.start.assert_called_once()
    scheduler_instance.shutdown.assert_called_once()
