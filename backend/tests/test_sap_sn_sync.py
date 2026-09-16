from datetime import date
from decimal import Decimal

import pytest

from app.integrations.sap_middleware import ExternalSnRecord, ExternalSnSnapshot
from app.models import ExternalSyncCheckpoint, JobRunLog, SapSnStaging, SapSnSyncBatch, SnAsset
from app.services import sap_sn_sync
from app.services.sap_sn_sync import (
    _apply_staging_row,
    advance_sn_sync_job,
    assess_sn_snapshot,
    snapshot_count_change_percent,
)


class _Rows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _ApplySession:
    def __init__(self, batch, staging, asset, checkpoint):
        self.batch = batch
        self.staging = staging
        self.asset = asset
        self.checkpoint = checkpoint
        self.execute_count = 0
        self.added = []

    async def get(self, model, identity, **_kwargs):
        return self.batch if model is SapSnSyncBatch and identity == self.batch.id else None

    async def execute(self, _statement):
        self.execute_count += 1
        if self.execute_count == 1:
            return _Rows([self.staging])
        return _Rows([self.asset])

    async def scalar(self, _statement):
        return self.checkpoint

    def add(self, value):
        self.added.append(value)


def _record(
    sn: str,
    customer: str = "CM1",
    material: str = "MAT1",
    warranty: str | None = None,
    ins_id: int = 1,
) -> ExternalSnRecord:
    return ExternalSnRecord(
        sn=sn,
        ins_id=ins_id,
        customer_code=customer,
        customer_name="Customer",
        material_code=material,
        values={"warranty_end_date": warranty},
    )


def test_snapshot_rejects_duplicates_and_required_field_gaps() -> None:
    result = assess_sn_snapshot([_record("SN1"), _record("SN1"), _record("SN2", customer="")])
    # Both SAP master rows for SN1 are retained; SN2 is invalid.
    assert result["duplicate_sns"] == {"SN1"}
    assert result["duplicate_count"] == 1
    assert [row.sn for row in result["invalid"]] == ["SN2"]
    assert result["valid_count"] == 2
    assert [row.sn for row in result["resolved"]] == ["SN1", "SN1"]


def test_snapshot_retains_all_warranty_rows() -> None:
    result = assess_sn_snapshot(
        [
            _record("SN1", warranty="2026-01-01"),
            _record("SN1", warranty="2027-06-30"),
            _record("SN1", warranty="2026-12-31"),
        ]
    )
    assert result["valid_count"] == 3
    assert [row.values["warranty_end_date"] for row in result["resolved"]] == [
        "2026-01-01", "2027-06-30", "2026-12-31"
    ]
    assert result["duplicate_sns"] == {"SN1"}
    assert result["duplicate_count"] == 2


def test_snapshot_nulls_are_both_retained() -> None:
    first = _record("SN1", customer="CM_A")
    second = _record("SN1", customer="CM_B")
    result = assess_sn_snapshot([first, second])
    assert result["resolved"] == [first, second]
    assert result["valid_count"] == 2
    assert result["duplicate_count"] == 1


def test_snapshot_mixed_invalid_and_duplicate() -> None:
    result = assess_sn_snapshot(
        [
            _record("SN1", warranty="2026-01-01"),
            _record("SN1", warranty="2027-06-30"),
            _record("SN2", customer=""),
        ]
    )
    assert [row.sn for row in result["invalid"]] == ["SN2"]
    assert [row.sn for row in result["resolved"]] == ["SN1", "SN1"]
    assert result["duplicate_sns"] == {"SN1"}
    assert result["valid_count"] == 2


def test_snapshot_duplicate_count_is_retained_extra_rows() -> None:
    result = assess_sn_snapshot([_record("SN1"), _record("SN1"), _record("SN1")])
    assert result["duplicate_count"] == 2
    assert result["valid_count"] == 3
    assert result["duplicate_sns"] == {"SN1"}


def test_snapshot_reports_duplicate_sap_row_identity() -> None:
    result = assess_sn_snapshot([
        _record("SN1", ins_id=99),
        _record("SN2", ins_id=99),
    ])
    assert result["duplicate_ins_ids"] == {99}


def test_snapshot_count_change_guard_uses_absolute_five_percent_boundary() -> None:
    assert snapshot_count_change_percent(None, 100) is None
    assert snapshot_count_change_percent(100, 105) == Decimal("5.0000")
    assert snapshot_count_change_percent(100, 94) == Decimal("6.0000")


@pytest.mark.anyio
async def test_chunked_sync_persists_cursor_between_source_pages(monkeypatch) -> None:
    checkpoint = ExternalSyncCheckpoint(id=7, sync_name="sqlserver_sn_assets")
    scalar_results = iter((checkpoint, None))
    added = []

    class Session:
        async def scalar(self, _statement):
            return next(scalar_results)

        async def flush(self):
            for value in added:
                if isinstance(value, SapSnSyncBatch) and value.id is None:
                    value.id = 11
            return None

        async def get(self, model, identity, **_kwargs):
            if model is SapSnSyncBatch and identity == 11:
                return next(value for value in added if isinstance(value, SapSnSyncBatch))
            return None

        def add(self, value):
            added.append(value)

    class Adapter:
        async def inspect_sn_snapshot(self):
            return ExternalSnSnapshot(source_count=2, max_ins_id=102)

        async def fetch_sn_records_page(self, **kwargs):
            assert kwargs == {"after_ins_id": None, "max_ins_id": 102, "limit": 500}
            return [_record("SN101", ins_id=101), _record("SN102", ins_id=102)]

    monkeypatch.setattr(sap_sn_sync, "create_sap_middleware_adapter", Adapter)
    session = Session()
    job = JobRunLog(id=5, metadata_json={"user_id": 3})

    initialized = await advance_sn_sync_job(session, job=job, user_id=3)
    assert initialized == {"status": "chunk_pending", "phase": "stage", "batch_id": 11}
    assert job.metadata_json["sync_snapshot"]["source_count"] == 2

    staged = await advance_sn_sync_job(session, job=job, user_id=3)
    assert staged["phase"] == "validate"
    assert job.metadata_json["sync_cursor"] == 102
    assert job.processed_count == 2
    assert len([value for value in added if isinstance(value, SapSnStaging)]) == 2


def test_apply_sync_is_idempotent_by_sqlserver_ins_id() -> None:
    batch = SapSnSyncBatch(
        id=1,
        batch_no="B1",
        status="syncing",
        source_count=1,
        valid_count=1,
        invalid_count=0,
        duplicate_count=0,
        snapshot_hash="a" * 64,
    )
    staging = SapSnStaging(
        id=2,
        sync_batch_id=1,
        ins_id=99,
        sn="SN-NEW",
        customer_code="CM2",
        customer_name="New customer",
        material_code="Z.SM.XA",
        material_name="New material",
        asset_status="valid",
        values_json={"warranty_end_date": "2027-01-02T00:00:00"},
        raw_data={"insID": 99},
        row_hash="b" * 64,
    )
    asset = SnAsset(
        id=3,
        ins_id=99,
        sn="SN-OLD",
        customer_code="CM1",
        customer_name="Old customer",
        material_code="Z.SM.OLD",
        asset_status="valid",
        source_system="sqlserver",
        external_id="99",
    )
    checkpoint = ExternalSyncCheckpoint(id=4, sync_name="sqlserver_sn_assets")
    session = _ApplySession(batch, staging, asset, checkpoint)

    _apply_staging_row(session, asset, staging, batch)

    assert session.added == []
    assert asset.id == 3
    assert asset.sn == "SN-NEW"
    assert asset.customer_code == "CM2"
    assert asset.warranty_end_date == date(2027, 1, 2)
    assert asset.external_id == "99"
