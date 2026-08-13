from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from tools import audit_langgraph_release


def test_release_audit_accepts_safe_legacy_configuration(monkeypatch) -> None:
    monkeypatch.setattr(
        audit_langgraph_release,
        "settings",
        Settings(_env_file=None, WORKFLOW_ENGINE="legacy"),
    )
    report = audit_langgraph_release.configuration_report()
    assert report["failures"] == []
    assert report["checkpoint_configured"] is False
    assert report["release_evidence_configured"] is False
    assert report["release_commit_configured"] is False
    assert report["release_evidence_max_age_hours"] == 168


def test_release_audit_rejects_unsafe_checkpoint_runtime_flags(monkeypatch) -> None:
    monkeypatch.setattr(
        audit_langgraph_release,
        "settings",
        Settings(
            _env_file=None,
            WORKFLOW_ENGINE="langgraph",
            LANGGRAPH_CHECKPOINT_DATABASE_URL="postgresql://checkpoint/graph",
            LANGGRAPH_STRICT_MSGPACK=False,
            LANGGRAPH_CHECKPOINT_AUTO_SETUP=True,
        ),
    )
    report = audit_langgraph_release.configuration_report()
    assert report["failures"] == ["STRICT_MSGPACK_DISABLED", "CHECKPOINT_AUTO_SETUP_ENABLED"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_checkpoint_probe_guard_rejects_remote_or_non_test_database() -> None:
    with pytest.raises(ValueError, match="LOCALHOST"):
        await audit_langgraph_release.checkpoint_probe("postgresql://db.example.com/checkpoint_test")
    with pytest.raises(ValueError, match="TEST_DATABASE"):
        await audit_langgraph_release.checkpoint_probe("postgresql://localhost/checkpoint")


@pytest.mark.anyio
async def test_job_lease_probe_guard_rejects_remote_or_wrong_database() -> None:
    with pytest.raises(ValueError, match="LOCALHOST"):
        await audit_langgraph_release.job_lease_probe(
            "mysql+asyncmy://user:secret@db.example.com/repair_system_test"
        )
    with pytest.raises(ValueError, match="REPAIR_SYSTEM_TEST"):
        await audit_langgraph_release.job_lease_probe(
            "mysql+asyncmy://user:secret@localhost/repair_system"
        )
    with pytest.raises(ValueError, match="LOCALHOST"):
        await audit_langgraph_release.email_dispatch_probe(
            "mysql+asyncmy://user:secret@db.example.com/repair_system_test"
        )
    with pytest.raises(ValueError, match="REPAIR_SYSTEM_TEST"):
        await audit_langgraph_release.email_dispatch_probe(
            "mysql+asyncmy://user:secret@localhost/repair_system"
        )


@pytest.mark.anyio
async def test_runtime_audit_requires_job_lease_smoke_url_when_probe_requested(monkeypatch) -> None:
    monkeypatch.setattr(
        audit_langgraph_release,
        "settings",
        Settings(_env_file=None, WORKFLOW_ENGINE="legacy", DB_SMOKE_DATABASE_URL=""),
    )

    report = await audit_langgraph_release.runtime_audit(
        probe_local_test_checkpoint=False,
        probe_local_test_job_lease=True,
    )

    assert report["audit_failures"] == ["JOB_LEASE_SMOKE_URL_MISSING"]
    assert report["requested_checks_passed"] is False
    assert report["local_graph_release_gate_passed"] is False
    assert report["production_signoff_complete"] is False


@pytest.mark.anyio
async def test_runtime_audit_requires_email_dispatch_smoke_url_when_probe_requested(monkeypatch) -> None:
    monkeypatch.setattr(
        audit_langgraph_release,
        "settings",
        Settings(_env_file=None, WORKFLOW_ENGINE="legacy", DB_SMOKE_DATABASE_URL=""),
    )

    report = await audit_langgraph_release.runtime_audit(
        probe_local_test_checkpoint=False,
        probe_local_test_email_dispatch=True,
    )

    assert report["audit_failures"] == ["EMAIL_DISPATCH_SMOKE_URL_MISSING"]
    assert report["requested_checks_passed"] is False
    assert report["local_graph_release_gate_passed"] is False


@pytest.mark.anyio
async def test_graph_release_gate_requires_all_three_local_probes(monkeypatch) -> None:
    monkeypatch.setattr(
        audit_langgraph_release,
        "settings",
        Settings(
            _env_file=None,
            WORKFLOW_ENGINE="langgraph",
            LANGGRAPH_CHECKPOINT_DATABASE_URL="postgresql://checkpoint/graph",
            LANGGRAPH_CHECKPOINT_SMOKE_DATABASE_URL="postgresql://localhost/checkpoint_test",
            DB_SMOKE_DATABASE_URL="mysql+asyncmy://localhost/repair_system_test",
        ),
    )
    monkeypatch.setattr(
        audit_langgraph_release,
        "business_schema_probe",
        lambda: _async_result({"revision_current": True, "missing_workflow_columns": {}, "ready": True}),
    )
    monkeypatch.setattr(
        audit_langgraph_release,
        "checkpoint_probe",
        lambda _url: _async_result({"connected": True}),
    )
    monkeypatch.setattr(
        audit_langgraph_release,
        "job_lease_probe",
        lambda _url: _async_result({"connected": True, "passed": True}),
    )
    monkeypatch.setattr(
        audit_langgraph_release,
        "email_dispatch_probe",
        lambda _url: _async_result({"connected": True, "passed": True}),
    )

    without_dispatch = await audit_langgraph_release.runtime_audit(
        probe_local_test_checkpoint=True,
        probe_local_test_job_lease=True,
    )
    complete_local = await audit_langgraph_release.runtime_audit(
        probe_local_test_checkpoint=True,
        probe_local_test_job_lease=True,
        probe_local_test_email_dispatch=True,
    )

    assert without_dispatch["audit_passed"] is True
    assert without_dispatch["local_graph_release_gate_passed"] is False
    assert complete_local["local_graph_release_gate_passed"] is True
    assert complete_local["production_signoff_complete"] is False


@pytest.mark.anyio
async def test_no_probe_audit_cannot_be_misread_as_complete_graph_release(monkeypatch) -> None:
    monkeypatch.setattr(
        audit_langgraph_release,
        "settings",
        Settings(_env_file=None, WORKFLOW_ENGINE="legacy"),
    )

    report = await audit_langgraph_release.runtime_audit(
        probe_local_test_checkpoint=False,
        probe_local_test_job_lease=False,
    )

    assert report["schema_version"] == 2
    assert report["requested_probes"] == {
        "local_test_checkpoint": False,
        "local_test_job_lease": False,
        "local_test_email_dispatch": False,
    }
    assert report["audit_passed"] is True
    assert report["result_scope"] == "requested_checks_only"
    assert report["requested_checks_passed"] is True
    assert report["local_graph_release_gate_passed"] is False
    assert report["production_signoff_complete"] is False


def test_release_evidence_writer_creates_verifiable_sidecar_without_credentials(tmp_path) -> None:
    output = tmp_path / "langgraph-release.json"
    report = {
        "configuration": {
            "checkpoint_configured": True,
            "checkpoint_host": "localhost",
        },
        "audit_passed": True,
    }

    evidence = audit_langgraph_release._write_evidence(output, report)

    payload = output.read_bytes()
    assert json.loads(payload) == report
    assert evidence["sha256"] == hashlib.sha256(payload).hexdigest()
    assert (tmp_path / "langgraph-release.json.sha256").read_text(encoding="ascii") == (
        f"{evidence['sha256']}  langgraph-release.json\n"
    )
    serialized = payload.decode("utf-8")
    assert "postgresql://" not in serialized
    assert "mysql+" not in serialized


def _valid_gate_report(*, commit: str = "abc123", dirty: bool = False) -> dict:
    return {
        "schema_version": 2,
        "collected_at": "2026-08-13T00:00:00+00:00",
        "source": {"commit": commit, "dirty": dirty},
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


def test_release_evidence_verifier_binds_clean_source_and_commit(tmp_path) -> None:
    output = tmp_path / "release.json"
    report = _valid_gate_report()
    audit_langgraph_release._write_evidence(output, report)

    verified = audit_langgraph_release.verify_local_release_evidence(
        output,
        expected_commit="abc123",
        allowed_root=tmp_path,
        max_age_hours=168,
        now=datetime(2026, 8, 13, 1, tzinfo=timezone.utc),
    )

    assert verified["verified"] is True
    assert verified["source_commit"] == "abc123"
    assert verified["source_dirty"] is False


@pytest.mark.parametrize(
    ("report", "expected_commit", "error"),
    [
        (_valid_gate_report(dirty=True), "abc123", "SOURCE_DIRTY"),
        (_valid_gate_report(commit="old"), "abc123", "COMMIT_MISMATCH"),
        ({**_valid_gate_report(), "schema_version": 1}, "abc123", "SCHEMA_UNSUPPORTED"),
        (
            {
                **_valid_gate_report(),
                "requested_probes": {
                    "local_test_checkpoint": True,
                    "local_test_job_lease": True,
                    "local_test_email_dispatch": False,
                },
            },
            "abc123",
            "PROBES_INCOMPLETE",
        ),
        ({**_valid_gate_report(), "local_graph_release_gate_passed": False}, "abc123", "GATE_FAILED"),
    ],
)
def test_release_evidence_verifier_rejects_unqualified_report(
    tmp_path,
    report: dict,
    expected_commit: str,
    error: str,
) -> None:
    output = tmp_path / "release.json"
    audit_langgraph_release._write_evidence(output, report)

    with pytest.raises(ValueError, match=error):
        audit_langgraph_release.verify_local_release_evidence(
            output,
            expected_commit=expected_commit,
        )


def test_release_evidence_verifier_rejects_tampering(tmp_path) -> None:
    output = tmp_path / "release.json"
    audit_langgraph_release._write_evidence(output, _valid_gate_report())
    output.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="DIGEST_MISMATCH"):
        audit_langgraph_release.verify_local_release_evidence(output)


def test_release_evidence_verifier_rejects_path_outside_trusted_root(tmp_path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside.json"
    audit_langgraph_release._write_evidence(outside, _valid_gate_report())

    with pytest.raises(ValueError, match="OUTSIDE_TRUSTED_ROOT"):
        audit_langgraph_release.verify_local_release_evidence(
            outside,
            expected_commit="abc123",
            allowed_root=trusted,
        )


@pytest.mark.parametrize(
    "arguments",
    [
        ["audit", "--expected-commit", "abc123"],
        ["audit", "--evidence-root", "/tmp/evidence"],
        ["audit", "--max-evidence-age-hours", "0", "--verify-local-release-evidence", "missing.json"],
    ],
)
def test_release_audit_cli_rejects_orphaned_or_invalid_verification_options(
    monkeypatch,
    arguments: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", arguments)

    with pytest.raises(SystemExit) as exc_info:
        audit_langgraph_release.main()

    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("collected_at", "error"),
    [
        (None, "TIMESTAMP_MISSING"),
        ("not-a-date", "TIMESTAMP_INVALID"),
        ("2026-08-01T00:00:00+00:00", "EXPIRED"),
        ("2026-08-14T00:00:00+00:00", "TIMESTAMP_IN_FUTURE"),
    ],
)
def test_release_evidence_verifier_rejects_invalid_or_stale_timestamp(
    tmp_path,
    collected_at: str | None,
    error: str,
) -> None:
    report = _valid_gate_report()
    if collected_at is None:
        report.pop("collected_at")
    else:
        report["collected_at"] = collected_at
    output = tmp_path / "release.json"
    audit_langgraph_release._write_evidence(output, report)

    with pytest.raises(ValueError, match=error):
        audit_langgraph_release.verify_local_release_evidence(
            output,
            expected_commit="abc123",
            allowed_root=tmp_path,
            max_age_hours=168,
            now=datetime(2026, 8, 13, 1, tzinfo=timezone.utc),
        )


@pytest.mark.anyio
async def test_graph_runtime_audit_requires_current_business_schema(monkeypatch) -> None:
    monkeypatch.setattr(
        audit_langgraph_release,
        "settings",
        Settings(
            _env_file=None,
            WORKFLOW_ENGINE="langgraph",
            LANGGRAPH_CHECKPOINT_DATABASE_URL="postgresql://checkpoint/graph",
        ),
    )
    monkeypatch.setattr(
        audit_langgraph_release,
        "business_schema_probe",
        lambda: _async_result(
            {
                "revision_current": False,
                "missing_workflow_columns": {"workflow_interrupts": ["checkpoint_step"]},
                "ready": False,
            }
        ),
    )

    report = await audit_langgraph_release.runtime_audit(probe_local_test_checkpoint=False)

    assert report["audit_failures"] == [
        "BUSINESS_SCHEMA_REVISION_NOT_CURRENT",
        "BUSINESS_WORKFLOW_SCHEMA_MISSING",
    ]


@pytest.mark.anyio
async def test_legacy_runtime_audit_does_not_require_graph_business_schema(monkeypatch) -> None:
    monkeypatch.setattr(
        audit_langgraph_release,
        "settings",
        Settings(_env_file=None, WORKFLOW_ENGINE="legacy"),
    )
    probe = pytest.fail
    monkeypatch.setattr(audit_langgraph_release, "business_schema_probe", probe)

    report = await audit_langgraph_release.runtime_audit(probe_local_test_checkpoint=False)

    assert report["audit_passed"] is True


async def _async_result(value):
    return value
