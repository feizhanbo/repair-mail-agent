from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def verify_local_release_evidence(
    path: Path,
    *,
    expected_commit: str | None = None,
    allowed_root: Path | None = None,
    max_age_hours: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify one schema-v2 pre-production gate artifact and its sidecar."""
    resolved = path.resolve()
    digest_path = resolved.with_name(f"{resolved.name}.sha256")
    if allowed_root is not None:
        trusted_root = allowed_root.resolve()
        try:
            resolved.relative_to(trusted_root)
            digest_path.resolve().relative_to(trusted_root)
        except ValueError as exc:
            raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_OUTSIDE_TRUSTED_ROOT") from exc
    if not resolved.is_file() or not digest_path.is_file():
        raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_MISSING")
    payload = resolved.read_bytes()
    expected_parts = digest_path.read_text(encoding="ascii").strip().split()
    if len(expected_parts) != 2 or expected_parts[1] != resolved.name:
        raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_DIGEST_INVALID")
    actual_digest = hashlib.sha256(payload).hexdigest()
    if expected_parts[0].lower() != actual_digest:
        raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_DIGEST_MISMATCH")
    try:
        report = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_JSON_INVALID") from exc
    if not isinstance(report, dict) or report.get("schema_version") != 2:
        raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_SCHEMA_UNSUPPORTED")
    if report.get("requested_probes") != {
        "local_test_checkpoint": True,
        "local_test_job_lease": True,
        "local_test_email_dispatch": True,
    }:
        raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_PROBES_INCOMPLETE")
    if (
        report.get("audit_passed") is not True
        or report.get("requested_checks_passed") is not True
        or report.get("local_graph_release_gate_passed") is not True
    ):
        raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_GATE_FAILED")
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    source_commit = str(source.get("commit") or "")
    if source.get("dirty") is not False:
        raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_SOURCE_DIRTY")
    if expected_commit is not None and source_commit != expected_commit:
        raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_COMMIT_MISMATCH")
    if max_age_hours is not None:
        collected_at = report.get("collected_at")
        if not isinstance(collected_at, str):
            raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_TIMESTAMP_MISSING")
        try:
            collected = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_TIMESTAMP_INVALID") from exc
        if collected.tzinfo is None:
            raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_TIMESTAMP_INVALID")
        checked_at = now or datetime.now(timezone.utc)
        age = checked_at - collected.astimezone(timezone.utc)
        if age.total_seconds() < -300:
            raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_TIMESTAMP_IN_FUTURE")
        if age > timedelta(hours=max_age_hours):
            raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_EXPIRED")
    return {
        "verified": True,
        "schema_version": 2,
        "sha256": actual_digest,
        "source_commit": source_commit,
        "source_dirty": False,
    }


def verify_runtime_release_gate(runtime_settings: Any) -> dict[str, Any] | None:
    """Fail closed before any Graph-capable API worker starts."""
    if runtime_settings.WORKFLOW_ENGINE != "langgraph":
        return None
    evidence_file = str(runtime_settings.LANGGRAPH_RELEASE_EVIDENCE_FILE or "").strip()
    release_commit = str(runtime_settings.APP_RELEASE_COMMIT or "").strip()
    evidence_root = str(runtime_settings.LANGGRAPH_RELEASE_EVIDENCE_ROOT or "").strip()
    if not evidence_file:
        raise RuntimeError("LANGGRAPH_RELEASE_EVIDENCE_FILE_REQUIRED")
    if not release_commit:
        raise RuntimeError("APP_RELEASE_COMMIT_REQUIRED")
    if not evidence_root:
        raise RuntimeError("LANGGRAPH_RELEASE_EVIDENCE_ROOT_REQUIRED")
    try:
        return verify_local_release_evidence(
            Path(evidence_file),
            expected_commit=release_commit,
            allowed_root=Path(evidence_root),
            max_age_hours=int(runtime_settings.LANGGRAPH_RELEASE_EVIDENCE_MAX_AGE_HOURS),
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
