from __future__ import annotations

import argparse
import asyncio
import socket
import uuid
from datetime import timedelta

from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.sqlalchemy_compat import configure_asyncmy_pre_ping
from app.integrations.sap_middleware import ExternalSnRecord, ExternalSnSnapshot
from app.models import (
    ExternalSyncCheckpoint,
    JobRunLog,
    SapSnSyncBatch,
    SnAsset,
    SystemEventLog,
    WorkerLease,
)
from app.services import sap_sn_sync
from app.services.common import utcnow
from app.services.jobs import (
    claim_next_job,
    enqueue_job,
    enqueue_job_or_retry_terminal,
    mark_interrupted_job,
    recover_stale_jobs,
)
from app.services.worker_lease import (
    WorkerLeaseUnavailable,
    acquire_worker_lease,
)


VERIFY_DATABASE = "airma_single_worker_verify"


async def _verify_concurrent_idempotent_enqueue(engine, sessions) -> None:
    key = f"fault.concurrent-enqueue.{uuid.uuid4().hex}"
    insert_started = asyncio.Event()

    def observe_insert(_conn, _cursor, statement, _parameters, _context, _executemany):
        normalized = statement.lstrip().lower()
        if normalized.startswith("insert into job_run_logs"):
            insert_started.set()

    winner_id: int | None = None
    event.listen(engine.sync_engine, "before_cursor_execute", observe_insert)
    try:
        async with sessions() as winner_session:
            winner = JobRunLog(
                job_name="ai_log_maintenance",
                job_type="ai_log_maintenance",
                status="queued",
                resource_type="fault_test",
                idempotency_key=key,
                priority=3,
                max_attempts=3,
                metadata_json={},
            )
            winner_session.add(winner)
            await winner_session.flush()
            winner_id = int(winner.id)
            insert_started.clear()

            async def enqueue_contender() -> int:
                async with sessions() as contender_session:
                    contender = await enqueue_job(
                        contender_session,
                        job_type="ai_log_maintenance",
                        resource_type="fault_test",
                        resource_id=None,
                        idempotency_key=key,
                    )
                    await contender_session.commit()
                    return int(contender.id)

            contender_task = asyncio.create_task(enqueue_contender())
            await asyncio.wait_for(insert_started.wait(), timeout=5)
            await winner_session.commit()
            contender_id = await asyncio.wait_for(contender_task, timeout=10)

        async with sessions() as session:
            row_count = await session.scalar(
                select(func.count()).select_from(JobRunLog).where(JobRunLog.idempotency_key == key)
            )
        assert contender_id == winner_id
        assert row_count == 1
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_insert)
        async with sessions.begin() as session:
            await session.execute(delete(JobRunLog).where(JobRunLog.idempotency_key == key))


async def _verify_updated_at_trigger(sessions) -> None:
    key = f"fault.updated_at.{uuid.uuid4().hex}"
    try:
        async with sessions.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO system_configs "
                    "(config_key,config_group,value_type,config_value,version) "
                    "VALUES (:key,'fault_test','bool','true',1)"
                ),
                {"key": key},
            )
        async with sessions() as session:
            before = await session.scalar(
                text("SELECT updated_at FROM system_configs WHERE config_key=:key"),
                {"key": key},
            )
        await asyncio.sleep(0.02)
        async with sessions.begin() as session:
            await session.execute(
                text("UPDATE system_configs SET version=version+1 WHERE config_key=:key"),
                {"key": key},
            )
        async with sessions() as session:
            after = await session.scalar(
                text("SELECT updated_at FROM system_configs WHERE config_key=:key"),
                {"key": key},
            )
        assert before is not None and after is not None and after > before
    finally:
        async with sessions.begin() as session:
            await session.execute(
                text("DELETE FROM system_configs WHERE config_key=:key"), {"key": key}
            )


async def _verify_chunked_sn_sync(sessions) -> None:
    base_ins_id = 1_500_000_000 + uuid.uuid4().int % 100_000_000
    records = [
        ExternalSnRecord(
            ins_id=base_ins_id + offset,
            sn=f"FAULT-SN-{base_ins_id + offset}",
            customer_code="FAULT-CUSTOMER",
            customer_name="Fault test",
            material_code="FAULT-MATERIAL",
            values={"asset_status": "valid"},
            raw_data={"fault_test": True},
        )
        for offset in (1, 2)
    ]

    class Adapter:
        async def inspect_sn_snapshot(self):
            return ExternalSnSnapshot(source_count=2, max_ins_id=base_ins_id + 2)

        async def fetch_sn_records_page(self, *, after_ins_id, max_ins_id, limit):
            assert max_ins_id == base_ins_id + 2 and limit == sap_sn_sync.SN_SYNC_CHUNK_SIZE
            return [record for record in records if after_ins_id is None or record.ins_id > after_ins_id]

    original_factory = sap_sn_sync.create_sap_middleware_adapter
    sap_sn_sync.create_sap_middleware_adapter = Adapter
    job_id: int | None = None
    batch_id: int | None = None
    try:
        async with sessions() as session:
            session.add(
                SnAsset(
                    ins_id=base_ins_id,
                    sn=f"FAULT-OLD-{base_ins_id}",
                    customer_code="FAULT-CUSTOMER",
                    material_code="FAULT-MATERIAL",
                    source_system="sqlserver",
                    external_id=str(base_ins_id),
                    asset_status="valid",
                )
            )
            job = JobRunLog(
                job_name="sap_sn_sync",
                job_type="sap_sn_sync",
                status="running",
                resource_type="fault_test_sn_sync",
                idempotency_key=f"fault-sn-sync:{base_ins_id}",
                priority=3,
                metadata_json={},
            )
            session.add(job)
            await session.flush()
            job_id = int(job.id)
            result = await sap_sn_sync.advance_sn_sync_job(session, job=job)
            assert result["phase"] == "stage"
            batch_id = int(result["batch_id"])
            await session.commit()

        expected_phases = ("validate", "apply", "apply", "finalize")
        for expected_phase in expected_phases:
            async with sessions() as session:
                job = await session.get(JobRunLog, job_id, with_for_update=True)
                assert job is not None
                result = await sap_sn_sync.advance_sn_sync_job(session, job=job)
                assert result["phase"] == expected_phase
                await session.commit()

        async with sessions() as session:
            job = await session.get(JobRunLog, job_id, with_for_update=True)
            assert job is not None
            result = await sap_sn_sync.advance_sn_sync_job(session, job=job)
            assert result["status"] == "succeeded"
            await session.commit()

        async with sessions() as session:
            batch = await session.get(SapSnSyncBatch, batch_id)
            assert batch is not None and batch.status == "succeeded"
            assert batch.source_count == 2 and batch.valid_count == 2
            assets = (
                await session.execute(
                    select(SnAsset)
                    .where(SnAsset.ins_id.in_([base_ins_id, base_ins_id + 1, base_ins_id + 2]))
                    .order_by(SnAsset.ins_id)
                )
            ).scalars().all()
            assert [asset.asset_status for asset in assets] == ["invalid", "valid", "valid"]
    finally:
        sap_sn_sync.create_sap_middleware_adapter = original_factory
        async with sessions() as session:
            if job_id is not None:
                await session.execute(delete(JobRunLog).where(JobRunLog.id == job_id))
            if batch_id is not None:
                await session.execute(delete(SapSnSyncBatch).where(SapSnSyncBatch.id == batch_id))
            await session.execute(
                delete(SnAsset).where(
                    SnAsset.ins_id.in_([base_ins_id, base_ins_id + 1, base_ins_id + 2])
                )
            )
            await session.execute(
                delete(ExternalSyncCheckpoint).where(
                    ExternalSyncCheckpoint.sync_name == sap_sn_sync.CHECKPOINT_NAME
                )
            )
            await session.commit()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run destructive single-worker fencing checks in the dedicated local verify DB"
    )
    parser.add_argument("--port", type=int, default=3307)
    parser.add_argument("--database", default=VERIFY_DATABASE)
    return parser.parse_args()


async def verify(*, port: int, database: str) -> None:
    if database != VERIFY_DATABASE:
        raise SystemExit(f"Refusing destructive verification outside {VERIFY_DATABASE}")
    source = make_url(settings.DATABASE_URL)
    if source.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Refusing destructive verification against a non-local MySQL host")
    url = source.set(port=port, database=database)
    engine = create_async_engine(url, pool_pre_ping=True, pool_recycle=300)
    configure_asyncmy_pre_ping(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = f"fault-{uuid.uuid4().hex[:12]}"
    old_instance = f"airma-test-old:{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    new_instance = f"airma-test-new:{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    lease_id: int | None = None
    job_id: int | None = None
    try:
        # Recover artifacts left by an interrupted prior run. This tool is
        # restricted to the dedicated disposable verification database.
        async with sessions() as session:
            prior_jobs = select(JobRunLog.id).where(JobRunLog.resource_type == "fault_test")
            await session.execute(
                delete(SystemEventLog).where(SystemEventLog.job_run_id.in_(prior_jobs))
            )
            await session.execute(
                delete(JobRunLog).where(JobRunLog.resource_type == "fault_test")
            )
            await session.execute(
                delete(WorkerLease).where(
                    WorkerLease.environment == "test",
                    WorkerLease.queue_name.like("fault-%"),
                )
            )
            await session.commit()

        async with sessions() as session:
            old_lease = await acquire_worker_lease(
                session,
                environment="test",
                queue_name=scope,
                instance_id=old_instance,
                app_version="fault-test-old",
                lease_seconds=60,
            )
            lease_id = old_lease.lease_id
            job = await enqueue_job(
                session,
                job_type="ai_log_maintenance",
                resource_type="fault_test",
                resource_id=None,
                idempotency_key=f"fault-test:{scope}",
                max_attempts=3,
            )
            await session.commit()
            job_id = int(job.id)

        async with sessions() as session:
            try:
                await acquire_worker_lease(
                    session,
                    environment="test",
                    queue_name=scope,
                    instance_id=new_instance,
                    app_version="fault-test-new",
                    lease_seconds=60,
                )
            except WorkerLeaseUnavailable:
                await session.rollback()
            else:
                raise AssertionError("second worker acquired a live lease")

        async with sessions() as session:
            claimed = await claim_next_job(
                session,
                worker_id=old_instance,
                fencing_token=old_lease.fencing_token,
            )
            assert claimed is not None and claimed.id == job_id
            assert claimed.execution_deadline_at is not None
            await session.commit()

        async with sessions() as session:
            await session.execute(
                update(WorkerLease)
                .where(WorkerLease.id == lease_id)
                .values(lease_expires_at=utcnow() - timedelta(seconds=1))
            )
            await session.commit()

        async with sessions() as session:
            new_lease = await acquire_worker_lease(
                session,
                environment="test",
                queue_name=scope,
                instance_id=new_instance,
                app_version="fault-test-new",
                lease_seconds=60,
            )
            assert new_lease.fencing_token == old_lease.fencing_token + 1
            await session.commit()

        async with sessions() as session:
            await session.execute(
                update(JobRunLog)
                .where(JobRunLog.id == job_id)
                .values(
                    locked_at=utcnow() - timedelta(hours=1),
                    execution_deadline_at=utcnow() - timedelta(seconds=1),
                )
            )
            recovered = await recover_stale_jobs(session)
            assert recovered == 1
            await session.commit()

        async with sessions() as session:
            reclaimed = await claim_next_job(
                session,
                worker_id=new_instance,
                fencing_token=new_lease.fencing_token,
            )
            assert reclaimed is not None and reclaimed.id == job_id
            assert reclaimed.fencing_token == new_lease.fencing_token
            await session.commit()

        async with sessions() as session:
            stale_changed = await mark_interrupted_job(
                session,
                job_id=job_id,
                worker_id=old_instance,
                fencing_token=old_lease.fencing_token,
                error_code="FAULT_TEST_STALE_OWNER",
            )
            assert stale_changed is False
            await session.rollback()

        async with sessions() as session:
            current_changed = await mark_interrupted_job(
                session,
                job_id=job_id,
                worker_id=new_instance,
                fencing_token=new_lease.fencing_token,
                error_code="FAULT_TEST_CURRENT_OWNER",
            )
            assert current_changed is True
            await session.commit()

        async with sessions() as session:
            final_job = await session.scalar(select(JobRunLog).where(JobRunLog.id == job_id))
            assert final_job is not None
            assert final_job.status == "retry_wait"
            assert final_job.fencing_token is None

        async with sessions() as session:
            smtp_parent = await enqueue_job(
                session,
                job_type="smtp_send",
                resource_type="fault_test",
                resource_id=None,
                idempotency_key=f"fault-test:{scope}:smtp",
                max_attempts=3,
            )
            await session.commit()
            smtp_parent_id = int(smtp_parent.id)
        async with sessions() as session:
            await session.execute(
                update(JobRunLog)
                .where(JobRunLog.id == smtp_parent_id)
                .values(status="failed", finished_at=utcnow(), error_code="FAULT_TEST")
            )
            await session.commit()
        async with sessions() as session:
            smtp_retry = await enqueue_job_or_retry_terminal(
                session,
                job_type="smtp_send",
                resource_type="fault_test",
                resource_id=None,
                idempotency_key=f"fault-test:{scope}:smtp",
                max_attempts=3,
            )
            await session.commit()
            smtp_retry_id = int(smtp_retry.id)
            assert smtp_retry.retry_of_job_id == smtp_parent_id
            assert smtp_retry_id != smtp_parent_id
        async with sessions() as session:
            same_retry = await enqueue_job_or_retry_terminal(
                session,
                job_type="smtp_send",
                resource_type="fault_test",
                resource_id=None,
                idempotency_key=f"fault-test:{scope}:smtp",
                max_attempts=3,
            )
            assert same_retry.id == smtp_retry_id
        await _verify_concurrent_idempotent_enqueue(engine, sessions)
        await _verify_chunked_sn_sync(sessions)
        await _verify_updated_at_trigger(sessions)
        print(
            {
                "status": "passed",
                "database": database,
                "second_worker_rejected": True,
                "takeover_token_incremented": True,
                "stale_owner_write_rejected": True,
                "expired_job_recovered": True,
                "terminal_idempotency_retry_lineage": True,
                "concurrent_idempotent_enqueue": True,
                "chunked_sn_sync_state_machine": True,
                "updated_at_database_trigger": True,
            }
        )
    finally:
        async with sessions() as session:
            fault_jobs = select(JobRunLog.id).where(JobRunLog.resource_type == "fault_test")
            await session.execute(
                delete(SystemEventLog).where(SystemEventLog.job_run_id.in_(fault_jobs))
            )
            await session.execute(
                delete(JobRunLog).where(JobRunLog.resource_type == "fault_test")
            )
            if lease_id is not None:
                await session.execute(delete(WorkerLease).where(WorkerLease.id == lease_id))
            await session.commit()
        await engine.dispose()


if __name__ == "__main__":
    args = _arguments()
    asyncio.run(verify(port=args.port, database=args.database))
