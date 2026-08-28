from __future__ import annotations

from copy import copy
from datetime import datetime
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.models import ExportSap, JobRunLog, RepairTicket, RepairTicketItem, SnAsset
from app.services import sap_rma
from app.services import jobs
from app.config import settings
from app.integrations.sap_middleware import ExternalRmaResult
from app.integrations.sap_middleware.sqlserver import SqlServerSapMiddlewareAdapter
from app.services.sap_rma_mapping import (
    RMA1_ALL_FIELDS,
    RMA1_DATABASE_OWNED_FIELDS,
    RMA1_NULL_FIELDS,
    RMA1_OPTIONAL_MAPPED_FIELDS,
    RMA1_REQUIRED_FIELDS,
    RMA1_UNKNOWN_FIELDS,
    RmaSubmissionValidationError,
    build_rma_submission,
)


REQUEST_ID = "12345678-1234-4234-8234-1234567890ab"


def _inputs():
    ticket = RepairTicket(
        id=1,
        ticket_no="T-1",
        mailing_address="Shanghai",
        contact_phone="13800000000",
        contact_person="Alice",
        contact_email="alice@example.com",
        charge_status="free",
    )
    item = RepairTicketItem(
        id=2,
        ticket_id=1,
        line_no=1,
        sn="SN-001",
        sn_asset_id=3,
        failure_description="No output",
    )
    asset = SnAsset(
        id=3,
        ins_id=9001,
        sn="SN-001",
        customer_code="C0001",
        customer_name="Customer",
        material_code="ITEM-1",
        material_name="Board",
        asset_status="valid",
    )
    return ticket, item, asset


def test_mapping_matrix_covers_all_63_real_rma1_fields() -> None:
    classified = (
        set(RMA1_REQUIRED_FIELDS)
        | set(RMA1_OPTIONAL_MAPPED_FIELDS)
        | set(RMA1_NULL_FIELDS)
        | set(RMA1_UNKNOWN_FIELDS)
        | set(RMA1_DATABASE_OWNED_FIELDS)
    )
    assert len(RMA1_ALL_FIELDS) == 63
    assert classified == set(RMA1_ALL_FIELDS)


@pytest.mark.parametrize(
    "field",
    ["RequestID", "internalSN", "itemCode", "customer", "BPBillAddr", "BPCellular", "insID"],
)
def test_every_required_rma1_field_blocks_submission_when_missing(field: str) -> None:
    ticket, item, asset = _inputs()
    request_id = REQUEST_ID
    if field == "RequestID":
        request_id = ""
    elif field == "internalSN":
        asset.sn = ""
        item.sn = ""
    elif field == "itemCode":
        asset.material_code = ""
    elif field == "customer":
        asset.customer_code = ""
    elif field == "BPBillAddr":
        ticket.mailing_address = ""
    elif field == "BPCellular":
        ticket.contact_phone = ""
    elif field == "insID":
        asset.ins_id = None

    with pytest.raises(RmaSubmissionValidationError):
        build_rma_submission(
            request_id=request_id,
            ticket=ticket,
            item=item,
            asset=asset,
            policy={},
        )


def test_optional_fields_use_real_values_or_explicit_none() -> None:
    ticket, item, asset = _inputs()
    asset.customer_name = None
    asset.material_name = None
    item.failure_description = None
    ticket.problem_description = None
    ticket.contact_email = None
    dto = build_rma_submission(
        request_id=REQUEST_ID,
        ticket=ticket,
        item=item,
        asset=asset,
        policy={},
    )
    assert dto.sql_parameters["custmrName"] is None
    assert dto.sql_parameters["itemName"] is None
    assert dto.sql_parameters["U_FailurePhenomena"] is None
    assert dto.sql_parameters["BPE_Mail"] is None
    assert dto.sql_parameters["insID"] == 9001
    assert set(dto.sql_parameters) == set(RMA1_REQUIRED_FIELDS) | set(RMA1_OPTIONAL_MAPPED_FIELDS)


def test_request_id_must_be_canonical_uuid_v4_char36() -> None:
    ticket, item, asset = _inputs()
    dto = build_rma_submission(
        request_id=REQUEST_ID,
        ticket=ticket,
        item=item,
        asset=asset,
        policy={},
    )
    assert len(str(dto.request_id)) == 36
    assert dto.request_id.version == 4
    with pytest.raises(RmaSubmissionValidationError):
        build_rma_submission(
            request_id="123456781234423482341234567890ab",
            ticket=copy(ticket),
            item=copy(item),
            asset=copy(asset),
            policy={},
        )


class _Rows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _BatchPollSession:
    def __init__(self, export_ids, lines):
        self.results = [_Rows(export_ids), _Rows(lines)]

    async def execute(self, _statement):
        return self.results.pop(0)


@pytest.mark.anyio
async def test_global_poller_queries_all_request_ids_once_then_fans_out(monkeypatch) -> None:
    ids = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    lines = [
        ExportSap(relay_export_id=10, ticket_id=1, ticket_item_id=1, ticket_version=1, request_id=ids[0], payload_hash="1" * 64),
        ExportSap(relay_export_id=10, ticket_id=1, ticket_item_id=2, ticket_version=1, request_id=ids[1], payload_hash="2" * 64),
        ExportSap(relay_export_id=20, ticket_id=2, ticket_item_id=3, ticket_version=1, request_id=ids[2], payload_hash="3" * 64),
    ]

    class Adapter:
        def __init__(self):
            self.calls = []

        async def query_rma_results(self, request_ids):
            self.calls.append(list(request_ids))
            return [ExternalRmaResult(request_id=UUID(ids[0]), sn="SN-1", rma_no="2026082801")]

    adapter = Adapter()
    fanout = AsyncMock(side_effect=[{"status": "waiting_rma"}, {"status": "waiting_rma"}])
    monkeypatch.setattr(sap_rma, "create_sap_middleware_adapter", lambda: adapter)
    monkeypatch.setattr(sap_rma, "poll_export_batch", fanout)

    result = await sap_rma.poll_waiting_rma_results(_BatchPollSession([10, 20], lines))

    assert result["request_count"] == 3
    assert result["result_count"] == 1
    assert len(adapter.calls) == 1
    assert adapter.calls[0] == [UUID(value) for value in ids]
    assert [call.kwargs["export_id"] for call in fanout.await_args_list] == [10, 20]
    assert all(call.kwargs["prefetched_results"] for call in fanout.await_args_list)


@pytest.mark.anyio
async def test_submit_failed_background_job_uses_automatic_backoff(monkeypatch) -> None:
    now = datetime(2026, 8, 28, 1, 0, 0)
    job = JobRunLog(
        id=1,
        job_name="relay_ticket_export",
        job_type="relay_ticket_export",
        status="running",
        resource_type="ticket_relay_export",
        resource_id=10,
        idempotency_key="relay-ticket-export-test",
        attempt_count=1,
        max_attempts=5,
        success_count=0,
        failed_count=0,
        locked_by="test-worker",
        locked_at=now,
        started_at=now,
    )

    class Session:
        async def get(self, _model, _identity, **_kwargs):
            return job

    monkeypatch.setattr(
        jobs,
        "_execute_job_command",
        AsyncMock(return_value={"status": "submit_failed", "error_code": "SAP_BATCH_SUBMIT_FAILED"}),
    )
    monkeypatch.setattr(jobs, "log_system_event", AsyncMock())

    result = await jobs.execute_claimed_job(Session(), job)

    assert result.status == "retry_wait"
    assert result.next_run_at is not None
    assert result.error_code == "SAP_BATCH_SUBMIT_FAILED"
    assert result.failed_count == 1


def test_sqlserver_connection_applies_login_and_query_timeout(monkeypatch) -> None:
    connection = SimpleNamespace(timeout=None)
    calls = []

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return connection

    monkeypatch.setitem(sys.modules, "pyodbc", SimpleNamespace(connect=connect))
    monkeypatch.setattr(settings, "RELAY_TIMEOUT_SECONDS", 7)
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_ENABLED", True)
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_HOST", "sql.test")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_DATABASE", "RMA_MS")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_USER", "user")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_PASSWORD", "secret")
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_SN_TABLE", "oins_rma")
    monkeypatch.setattr(
        settings,
        "RELAY_SQLSERVER_SN_COLUMN_MAP",
        {
            "ins_id": "insID",
            "sn": "internalSN",
            "customer_code": "customer",
            "material_code": "ITEMCODE",
        },
    )

    result = SqlServerSapMiddlewareAdapter()._connect()

    assert result is connection
    assert result.timeout == 7
    assert calls[0][1]["timeout"] == 7
    assert calls[0][1]["autocommit"] is False


def test_rma2_query_uses_request_id_and_rma_number_not_u_status(monkeypatch) -> None:
    request_id = uuid4()

    class Cursor:
        sql = ""

        def execute(self, sql, _parameters):
            self.sql = sql
            return self

        def fetchall(self):
            return [(str(request_id), "SN-1", "RMA-001", datetime(2026, 8, 28))]

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

        def close(self):
            return None

    adapter = SqlServerSapMiddlewareAdapter()
    monkeypatch.setattr(adapter, "_connect", lambda: Connection())

    results = adapter._query_ids([request_id], result=True)

    assert "[RequestID]" in cursor.sql
    assert "[U_CustomerNum]" in cursor.sql
    assert "U_Status" not in cursor.sql
    assert results[0].request_id == request_id
    assert results[0].rma_no == "RMA-001"
