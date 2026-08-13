from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import bindparam, delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Email, JobRunLog, SystemEventLog, WorkflowExecution, WorkflowInterrupt
from app.services import emails, jobs
from app.services.common import utcnow
from app.services.release_evidence import verify_local_release_evidence
from app.workflows.checkpoint import postgres_checkpointer
from app.workflows.executions import create_execution


REQUIRED_BUSINESS_REVISION = "r5m0h1c2d3e4"
REQUIRED_WORKFLOW_COLUMNS = {
    "workflow_executions": {
        "execution_id", "graph_thread_id", "workflow_version", "state_schema_version",
        "checkpoint_id", "checkpoint_step",
    },
    "workflow_interrupts": {"execution_id", "interrupt_id", "checkpoint_id", "checkpoint_step"},
}


def _git_identity() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[2]

    def run(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip()

    commit = run("rev-parse", "HEAD")
    branch = run("branch", "--show-current")
    status = run("status", "--porcelain")
    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(status) if status is not None else None,
    }


def _write_evidence(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    resolved = path.resolve()
    payload = (json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    digest_path = resolved.with_name(f"{resolved.name}.sha256")
    digest_path.write_bytes(f"{digest}  {resolved.name}\n".encode("ascii"))
    return {
        "path": str(resolved),
        "size_bytes": len(payload),
        "sha256": digest,
        "sha256_file": str(digest_path),
    }


def configuration_report() -> dict[str, Any]:
    checkpoint_url = settings.LANGGRAPH_CHECKPOINT_DATABASE_URL.strip()
    parsed = make_url(checkpoint_url) if checkpoint_url else None
    failures: list[str] = []
    if settings.WORKFLOW_ENGINE == "langgraph" and parsed is None:
        failures.append("CHECKPOINT_URL_MISSING")
    if parsed is not None and parsed.get_backend_name() != "postgresql":
        failures.append("CHECKPOINT_NOT_POSTGRESQL")
    if not settings.LANGGRAPH_STRICT_MSGPACK:
        failures.append("STRICT_MSGPACK_DISABLED")
    if settings.LANGGRAPH_CHECKPOINT_AUTO_SETUP:
        failures.append("CHECKPOINT_AUTO_SETUP_ENABLED")
    return {
        "workflow_engine": settings.WORKFLOW_ENGINE,
        "rollout_percent": settings.LANGGRAPH_ROLLOUT_PERCENT,
        "allowlist_count": len(settings.LANGGRAPH_EMAIL_ALLOWLIST),
        "checkpoint_configured": parsed is not None,
        "checkpoint_backend": parsed.get_backend_name() if parsed is not None else None,
        "checkpoint_host": parsed.host if parsed is not None else None,
        "strict_msgpack": settings.LANGGRAPH_STRICT_MSGPACK,
        "auto_setup": settings.LANGGRAPH_CHECKPOINT_AUTO_SETUP,
        "release_evidence_configured": bool(settings.LANGGRAPH_RELEASE_EVIDENCE_FILE.strip()),
        "release_evidence_root_configured": bool(settings.LANGGRAPH_RELEASE_EVIDENCE_ROOT.strip()),
        "release_commit_configured": bool(settings.APP_RELEASE_COMMIT.strip()),
        "release_evidence_max_age_hours": settings.LANGGRAPH_RELEASE_EVIDENCE_MAX_AGE_HOURS,
        "failures": failures,
    }


async def checkpoint_probe(database_url: str) -> dict[str, Any]:
    parsed = make_url(database_url)
    if parsed.host not in {"127.0.0.1", "localhost"}:
        raise ValueError("CHECKPOINT_PROBE_REQUIRES_LOCALHOST")
    if not str(parsed.database or "").endswith("_test"):
        raise ValueError("CHECKPOINT_PROBE_REQUIRES_TEST_DATABASE")
    async with postgres_checkpointer(database_url, strict_msgpack=True) as saver:
        # Listing an impossible thread proves connectivity and schema presence
        # without writing a checkpoint or requiring cleanup.
        rows = [item async for item in saver.alist({"configurable": {"thread_id": "release-audit-nonexistent"}}, limit=1)]
    return {"connected": True, "schema_query_succeeded": True, "unexpected_rows": len(rows)}


def _validated_local_test_mysql_url(database_url: str) -> str:
    parsed = make_url(database_url)
    if parsed.host not in {"127.0.0.1", "localhost"}:
        raise ValueError("JOB_LEASE_PROBE_REQUIRES_LOCALHOST")
    if not parsed.drivername.startswith("mysql+"):
        raise ValueError("JOB_LEASE_PROBE_REQUIRES_ASYNC_MYSQL")
    if parsed.database != "repair_system_test":
        raise ValueError("JOB_LEASE_PROBE_REQUIRES_REPAIR_SYSTEM_TEST")
    if parsed.drivername == "mysql+aiomysql":
        parsed = parsed.set(drivername="mysql+asyncmy")
    return parsed.render_as_string(hide_password=False)


async def job_lease_probe(database_url: str) -> dict[str, Any]:
    """Exercise real cross-session fencing without invoking a Graph or external side effect."""
    mysql_url = _validated_local_test_mysql_url(database_url)
    suffix = uuid4().hex
    idempotency_key = f"lease-release-probe:{suffix}"
    engine = create_async_engine(mysql_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    job_id: int | None = None
    first_token = ""
    second_token = ""
    try:
        async with sessions() as session:
            active_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(JobRunLog)
                    .where(JobRunLog.status.in_({"queued", "retry_wait", "running"}))
                )
                or 0
            )
            if active_count:
                raise RuntimeError("JOB_LEASE_PROBE_REQUIRES_IDLE_TEST_QUEUE")
            fixture = await jobs.enqueue_job(
                session,
                job_type="graph_start",
                resource_type="lease_release_probe",
                resource_id=None,
                idempotency_key=idempotency_key,
                metadata={"execution_id": f"lease-release-probe-{suffix}"},
            )
            await session.commit()
            job_id = fixture.id

        async with sessions() as claim_session:
            claimed = await jobs.claim_next_job(claim_session, worker_id="lease-release-probe")
            if claimed is None or claimed.id != job_id:
                raise RuntimeError("JOB_LEASE_PROBE_CLAIM_MISMATCH")
            first_token = str(claimed.locked_by or "")
            await claim_session.commit()

        async with sessions() as heartbeat_session:
            first_owner_renewed = await jobs.renew_job_lease(
                heartbeat_session,
                job_id=job_id,
                owner_token=first_token,
            )
            await heartbeat_session.commit()
        async with sessions() as wrong_owner_session:
            wrong_owner_rejected = not await jobs.renew_job_lease(
                wrong_owner_session,
                job_id=job_id,
                owner_token="superseded-owner",
            )
            await wrong_owner_session.rollback()

        async with sessions() as expire_session:
            row = await expire_session.get(JobRunLog, job_id, with_for_update=True)
            if row is None:
                raise RuntimeError("JOB_LEASE_PROBE_FIXTURE_MISSING")
            row.locked_at = utcnow() - timedelta(seconds=settings.ASYNC_JOB_STALE_SECONDS + 30)
            await expire_session.commit()

        async with sessions() as recovery_session:
            recovered_count = await jobs.recover_stale_jobs(recovery_session)
            await recovery_session.commit()
        async with sessions() as inspection_session:
            stale = await inspection_session.get(JobRunLog, job_id)
            graph_stale_failed_closed = bool(
                stale is not None
                and stale.status == "needs_manual_review"
                and stale.error_code == "GRAPH_JOB_LEASE_EXPIRED_UNCERTAIN"
                and stale.locked_by is None
                and stale.locked_at is None
            )

        async with sessions() as operator_session:
            reactivated = await jobs.reactivate_stale_graph_job(
                operator_session,
                job_id=job_id,
                operator_user_id=1,
                confirm_previous_worker_stopped=True,
                reason="synthetic release probe worker stopped",
            )
            operator_recovery_queued = reactivated.status == "queued"
            await operator_session.commit()

        async with sessions() as second_claim_session:
            reclaimed = await jobs.claim_next_job(second_claim_session, worker_id="lease-release-probe")
            if reclaimed is None or reclaimed.id != job_id:
                raise RuntimeError("JOB_LEASE_PROBE_RECLAIM_MISMATCH")
            second_token = str(reclaimed.locked_by or "")
            await second_claim_session.commit()
        async with sessions() as old_owner_session:
            old_owner_rejected = not await jobs.renew_job_lease(
                old_owner_session,
                job_id=job_id,
                owner_token=first_token,
            )
            await old_owner_session.rollback()
        async with sessions() as new_owner_session:
            new_owner_renewed = await jobs.renew_job_lease(
                new_owner_session,
                job_id=job_id,
                owner_token=second_token,
            )
            await new_owner_session.commit()

        checks = {
            "first_owner_renewed": first_owner_renewed,
            "wrong_owner_rejected": wrong_owner_rejected,
            "single_stale_recovered": recovered_count == 1,
            "graph_stale_failed_closed": graph_stale_failed_closed,
            "operator_recovery_queued": operator_recovery_queued,
            "owner_token_rotated": bool(first_token and second_token and first_token != second_token),
            "old_owner_rejected": old_owner_rejected,
            "new_owner_renewed": new_owner_renewed,
        }
        return {"connected": True, "checks": checks, "passed": all(checks.values())}
    finally:
        if job_id is not None:
            async with sessions() as cleanup_session:
                await cleanup_session.execute(
                    delete(SystemEventLog).where(SystemEventLog.job_run_id == job_id)
                )
                await cleanup_session.execute(delete(JobRunLog).where(JobRunLog.id == job_id))
                await cleanup_session.commit()
        await engine.dispose()


async def email_dispatch_probe(database_url: str) -> dict[str, Any]:
    """Prove that two MySQL sessions cannot queue parallel Graphs for one email."""
    mysql_url = _validated_local_test_mysql_url(database_url)
    suffix = uuid4().hex
    engine = create_async_engine(mysql_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    email_id: int | None = None
    job_id: int | None = None
    second_task: asyncio.Task | None = None
    second_started = asyncio.Event()
    try:
        async with sessions() as fixture_session:
            fixture = Email(
                mailbox_account="langgraph-release-probe@example.invalid",
                message_id=f"<langgraph-dispatch-probe-{suffix}@example.invalid>",
                from_address="probe@example.invalid",
                subject="LangGraph dispatch concurrency probe",
                text_body="Synthetic release probe; no external side effect.",
                parse_status="pending",
                processing_stage="fetched",
            )
            fixture_session.add(fixture)
            await fixture_session.commit()
            email_id = fixture.id

        async with sessions() as first_session, sessions() as second_session:
            first = await emails._dispatch_langgraph_email_parse(
                first_session,
                email_id=email_id,
                reason="synthetic concurrent dispatch probe",
            )
            job_id = int(first["workflow"]["job_id"])
            fixture_job = await first_session.get(JobRunLog, job_id)
            if fixture_job is None:
                raise RuntimeError("EMAIL_DISPATCH_PROBE_JOB_MISSING")
            # Keep this synthetic Graph ineligible for worker pickup even if
            # the dedicated test database was accidentally left with a poller.
            fixture_job.next_run_at = utcnow() + timedelta(hours=1)
            async def dispatch_second() -> dict[str, Any]:
                second_started.set()
                return await emails._dispatch_langgraph_email_parse(
                    second_session,
                    email_id=email_id,
                    reason="synthetic concurrent dispatch probe",
                )
            second_task = asyncio.create_task(dispatch_second())
            await asyncio.wait_for(second_started.wait(), timeout=1)
            await asyncio.sleep(0.2)
            second_blocked_before_commit = not second_task.done()
            await first_session.commit()
            second = await asyncio.wait_for(second_task, timeout=5)
            await second_session.commit()

        async with sessions() as inspection_session:
            graph_start_count = int(
                await inspection_session.scalar(
                    select(func.count())
                    .select_from(JobRunLog)
                    .where(
                        JobRunLog.job_type == "graph_start",
                        JobRunLog.resource_type == "email",
                        JobRunLog.resource_id == email_id,
                    )
                )
                or 0
            )
        execution_id = str(first["workflow"]["execution_id"])
        async with sessions() as failure_session:
            execution = await create_execution(
                failure_session,
                execution_id=execution_id,
                graph_thread_id=f"email-ticket-{execution_id}",
                email_id=email_id,
                trigger_job_id=job_id,
            )
            execution.status = "failed"
            execution.completed_at = utcnow()
            failed_job = await failure_session.get(JobRunLog, job_id, with_for_update=True)
            if failed_job is None:
                raise RuntimeError("EMAIL_DISPATCH_PROBE_JOB_MISSING")
            failed_job.status = "failed"
            failed_job.error_code = "SYNTHETIC_DISPATCH_PROBE_FAILURE"
            failed_job.finished_at = utcnow()
            await failure_session.commit()

        async with sessions() as recovery_session:
            recovered_job = await jobs.reactivate_failed_graph_start_job(
                recovery_session,
                job_id=job_id,
                operator_user_id=1,
                reason="synthetic release probe recovery",
            )
            failed_recovery_reused_job = bool(
                recovered_job.id == job_id
                and recovered_job.status == "queued"
                and str((recovered_job.metadata_json or {}).get("execution_id") or "") == execution_id
            )
            await recovery_session.commit()

        async with sessions() as recovered_inspection:
            recovered_execution = await recovered_inspection.scalar(
                select(WorkflowExecution).where(WorkflowExecution.execution_id == execution_id)
            )
            failed_recovery_kept_execution_identity = bool(
                recovered_execution is not None
                and recovered_execution.email_id == email_id
                and recovered_execution.trigger_job_id == job_id
                and recovered_execution.status == "failed"
            )
        checks = {
            "second_dispatch_blocked_on_email_lock": second_blocked_before_commit,
            "second_dispatch_reused_owner": second.get("status") == "workflow_active",
            "same_execution_id": first["workflow"]["execution_id"] == second["workflow"]["execution_id"],
            "same_job_id": first["workflow"]["job_id"] == second["workflow"]["job_id"],
            "single_graph_start_row": graph_start_count == 1,
            "failed_recovery_reused_job": failed_recovery_reused_job,
            "failed_recovery_kept_execution_identity": failed_recovery_kept_execution_identity,
        }
        return {"connected": True, "checks": checks, "passed": all(checks.values())}
    finally:
        if second_task is not None and not second_task.done():
            second_task.cancel()
            try:
                await second_task
            except asyncio.CancelledError:
                pass
        if email_id is not None:
            async with sessions() as cleanup_session:
                event_cleanup = SystemEventLog.email_id == email_id
                if job_id is not None:
                    event_cleanup = event_cleanup | (SystemEventLog.job_run_id == job_id)
                await cleanup_session.execute(
                    delete(SystemEventLog).where(event_cleanup)
                )
                await cleanup_session.execute(
                    delete(WorkflowInterrupt).where(WorkflowInterrupt.execution_id.in_(
                        select(WorkflowExecution.execution_id).where(WorkflowExecution.email_id == email_id)
                    ))
                )
                await cleanup_session.execute(
                    delete(WorkflowExecution).where(WorkflowExecution.email_id == email_id)
                )
                await cleanup_session.execute(
                    delete(JobRunLog).where(
                        JobRunLog.job_type == "graph_start",
                        JobRunLog.resource_type == "email",
                        JobRunLog.resource_id == email_id,
                    )
                )
                await cleanup_session.execute(delete(Email).where(Email.id == email_id))
                await cleanup_session.commit()
        await engine.dispose()


async def business_schema_probe() -> dict[str, Any]:
    from app.core.database import AsyncSessionLocal

    table_names = tuple(REQUIRED_WORKFLOW_COLUMNS)
    async with AsyncSessionLocal() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
        rows = (
            await session.execute(
                text(
                    "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name "
                    "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME IN :tables"
                ).bindparams(bindparam("tables", expanding=True)),
                {"tables": table_names},
            )
        ).mappings().all()
    actual: dict[str, set[str]] = {name: set() for name in table_names}
    for row in rows:
        actual[str(row["table_name"])].add(str(row["column_name"]))
    missing = {
        table: sorted(required - actual[table])
        for table, required in REQUIRED_WORKFLOW_COLUMNS.items()
        if required - actual[table]
    }
    return {
        "revision": str(revision) if revision is not None else None,
        "required_revision": REQUIRED_BUSINESS_REVISION,
        "revision_current": str(revision) == REQUIRED_BUSINESS_REVISION,
        "missing_workflow_columns": missing,
        "ready": str(revision) == REQUIRED_BUSINESS_REVISION and not missing,
    }


async def runtime_audit(
    *,
    probe_local_test_checkpoint: bool,
    probe_local_test_job_lease: bool = False,
    probe_local_test_email_dispatch: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": 2,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "source": _git_identity(),
        "configuration": configuration_report(),
        "requested_probes": {
            "local_test_checkpoint": probe_local_test_checkpoint,
            "local_test_job_lease": probe_local_test_job_lease,
            "local_test_email_dispatch": probe_local_test_email_dispatch,
        },
    }
    failures = list(report["configuration"]["failures"])
    if settings.WORKFLOW_ENGINE == "langgraph":
        try:
            report["business_schema"] = await business_schema_probe()
        except Exception as exc:
            failures.append("BUSINESS_SCHEMA_PROBE_FAILED")
            report["business_schema"] = {"ready": False, "error_code": exc.__class__.__name__}
        else:
            if not report["business_schema"]["revision_current"]:
                failures.append("BUSINESS_SCHEMA_REVISION_NOT_CURRENT")
            if report["business_schema"]["missing_workflow_columns"]:
                failures.append("BUSINESS_WORKFLOW_SCHEMA_MISSING")
    if probe_local_test_checkpoint:
        smoke_url = settings.LANGGRAPH_CHECKPOINT_SMOKE_DATABASE_URL.strip()
        if not smoke_url:
            failures.append("CHECKPOINT_SMOKE_URL_MISSING")
        else:
            try:
                report["checkpoint_probe"] = await checkpoint_probe(smoke_url)
            except Exception as exc:
                failures.append(exc.__class__.__name__)
                report["checkpoint_probe"] = {"connected": False, "error_code": exc.__class__.__name__}
    if probe_local_test_job_lease:
        smoke_url = settings.DB_SMOKE_DATABASE_URL.strip()
        if not smoke_url:
            failures.append("JOB_LEASE_SMOKE_URL_MISSING")
        else:
            try:
                report["job_lease_probe"] = await job_lease_probe(smoke_url)
            except Exception as exc:
                failures.append(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else exc.__class__.__name__)
                report["job_lease_probe"] = {"connected": False, "error_code": exc.__class__.__name__}
            else:
                if not report["job_lease_probe"]["passed"]:
                    failures.append("JOB_LEASE_PROBE_CHECK_FAILED")
    if probe_local_test_email_dispatch:
        smoke_url = settings.DB_SMOKE_DATABASE_URL.strip()
        if not smoke_url:
            failures.append("EMAIL_DISPATCH_SMOKE_URL_MISSING")
        else:
            try:
                report["email_dispatch_probe"] = await email_dispatch_probe(smoke_url)
            except Exception as exc:
                failures.append(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else exc.__class__.__name__)
                report["email_dispatch_probe"] = {"connected": False, "error_code": exc.__class__.__name__}
            else:
                if not report["email_dispatch_probe"]["passed"]:
                    failures.append("EMAIL_DISPATCH_PROBE_CHECK_FAILED")
    report["audit_failures"] = failures
    report["audit_passed"] = not failures
    report["result_scope"] = "requested_checks_only"
    report["requested_checks_passed"] = not failures
    report["local_graph_release_gate_passed"] = bool(
        settings.WORKFLOW_ENGINE == "langgraph"
        and probe_local_test_checkpoint
        and probe_local_test_job_lease
        and probe_local_test_email_dispatch
        and not failures
    )
    report["production_signoff_complete"] = False
    report["production_signoff_note"] = (
        "This tool cannot prove SAP/SMTP/OSS fault recovery, real-model/attachment gold results, "
        "business approval, or staged production rollout."
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit LangGraph release configuration and checkpoint readiness.")
    parser.add_argument(
        "--probe-local-test-checkpoint",
        action="store_true",
        help="Connect only to LANGGRAPH_CHECKPOINT_SMOKE_DATABASE_URL (localhost, *_test).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the credential-free JSON report to this evidence file.",
    )
    parser.add_argument(
        "--probe-local-test-job-lease",
        action="store_true",
        help="Exercise Job fencing only on DB_SMOKE_DATABASE_URL (localhost repair_system_test).",
    )
    parser.add_argument(
        "--probe-local-test-email-dispatch",
        action="store_true",
        help="Exercise concurrent email Graph dispatch only on DB_SMOKE_DATABASE_URL (localhost repair_system_test).",
    )
    parser.add_argument(
        "--verify-local-release-evidence",
        type=Path,
        help="Verify a schema-v2 three-probe evidence JSON and its .sha256 sidecar, then exit.",
    )
    parser.add_argument(
        "--expected-commit",
        help="Require verified release evidence to match this exact Git commit.",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        help="Require the evidence JSON and sidecar to resolve beneath this trusted root.",
    )
    parser.add_argument(
        "--max-evidence-age-hours",
        type=int,
        help="Reject verified release evidence older than this many hours.",
    )
    args = parser.parse_args()
    verification_options_used = any(
        value is not None
        for value in (
            args.expected_commit,
            args.evidence_root,
            args.max_evidence_age_hours,
        )
    )
    if args.verify_local_release_evidence is None and verification_options_used:
        parser.error("evidence verification options require --verify-local-release-evidence")
    if args.max_evidence_age_hours is not None and args.max_evidence_age_hours <= 0:
        parser.error("--max-evidence-age-hours must be positive")
    if args.verify_local_release_evidence is not None:
        try:
            verification = verify_local_release_evidence(
                args.verify_local_release_evidence,
                expected_commit=args.expected_commit,
                allowed_root=args.evidence_root,
                max_age_hours=args.max_evidence_age_hours,
            )
        except ValueError as exc:
            print(json.dumps({"verified": False, "error_code": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(verification, ensure_ascii=False, indent=2))
        return 0
    report = asyncio.run(
        runtime_audit(
            probe_local_test_checkpoint=args.probe_local_test_checkpoint,
            probe_local_test_job_lease=args.probe_local_test_job_lease,
            probe_local_test_email_dispatch=args.probe_local_test_email_dispatch,
        )
    )
    if args.output is not None:
        report["evidence_file"] = _write_evidence(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["audit_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
