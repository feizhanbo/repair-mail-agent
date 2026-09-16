from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import closing
from datetime import date, datetime
from typing import Any, Sequence
from uuid import UUID

from app.config import settings
from app.integrations.sap_middleware.contracts import (
    ConnectionHealth, ExternalRmaResult, ExternalRmaSubmissionItem, ExternalSnRecord,
    ExternalSnSnapshot,
    SapMiddlewareConfigurationError, SapSchemaMismatchError, SapSnapshotUnstableError,
    SapTransactionError, SapUnknownCommitStateError,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#@]*$")
logger = logging.getLogger(__name__)
_SN_LOCAL_FIELDS = {
    "ins_id", "sn", "customer_code", "customer_name", "material_code", "material_name",
    "service_tracking_card_no", "parent_sn", "top_sn", "parent_material_code",
    "top_material_code", "asset_status", "warranty_start_date", "warranty_end_date",
}


def _sqlserver_error_code(exc: BaseException) -> str:
    text = " ".join(str(v) for v in getattr(exc, "args", ())).upper()
    if any(v in text for v in ("28000", "18456", "LOGIN FAILED")): return "SQLSERVER_LOGIN_FAILED"
    if "LOGIN TIMEOUT" in text: return "SQLSERVER_CONNECTION_TIMEOUT"
    if any(v in text for v in ("NAME OR SERVICE NOT KNOWN", "TEMPORARY FAILURE IN NAME RESOLUTION", "DNS")): return "SQLSERVER_DNS_FAILED"
    if any(v in text for v in ("HYT00", "HYT01", "TIMEOUT")): return "SQLSERVER_QUERY_TIMEOUT"
    if any(v in text for v in ("08S01", "08001", "CONNECTION")): return "SQLSERVER_CONNECTION_FAILED"
    if any(v in text for v in ("40001", "1205", "DEADLOCK")): return "SQLSERVER_DEADLOCK"
    if any(v in text for v in ("2627", "2601", "DUPLICATE")): return "SQLSERVER_DUPLICATE"
    if any(v in text for v in ("23000", "CONSTRAINT")): return "SQLSERVER_CONSTRAINT_VIOLATION"
    if any(v in text for v in ("CERTIFICATE", "TLS", "SSL")): return "SQLSERVER_TLS_FAILED"
    if any(v in text for v in ("NO DATA FOUND", "NOT FOUND")): return "SQLSERVER_DATA_NOT_FOUND"
    if isinstance(exc, SapSchemaMismatchError): return "SQLSERVER_SCHEMA_MISMATCH"
    if isinstance(exc, SapUnknownCommitStateError): return "SQLSERVER_COMMIT_UNKNOWN"
    if isinstance(exc, SapTransactionError): return "SQLSERVER_TRANSACTION_ROLLBACK"
    return "SQLSERVER_OPERATION_FAILED"


def _identifier(value: str) -> str:
    if not value or not _IDENTIFIER.fullmatch(value):
        raise SapMiddlewareConfigurationError(f"SAP_IDENTIFIER_INVALID:{str(value)[:80]}")
    return f"[{value}]"


def _qualified(schema: str, name: str) -> str:
    return f"{_identifier(schema)}.{_identifier(name)}"


def _payload_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, datetime.min.time())
    return value


class SqlServerSapMiddlewareAdapter:
    def __init__(self, *, invalid_adapter: str | None = None):
        self.invalid_adapter = invalid_adapter

    def _missing(self) -> list[str]:
        if self.invalid_adapter:
            return [f"RELAY_ADAPTER_INVALID:{self.invalid_adapter}"]
        required = {
            "RELAY_SQLSERVER_ENABLED": settings.RELAY_SQLSERVER_ENABLED,
            "RELAY_SQLSERVER_HOST": settings.RELAY_SQLSERVER_HOST,
            "RELAY_SQLSERVER_DATABASE": settings.RELAY_SQLSERVER_DATABASE,
            "RELAY_SQLSERVER_USER": settings.RELAY_SQLSERVER_USER,
            "RELAY_SQLSERVER_PASSWORD": settings.RELAY_SQLSERVER_PASSWORD,
            "RELAY_SQLSERVER_DRIVER": settings.RELAY_SQLSERVER_DRIVER,
            "RELAY_SQLSERVER_SN_TABLE": settings.RELAY_SQLSERVER_SN_TABLE,
            "RELAY_SQLSERVER_REQUEST_TABLE": settings.RELAY_SQLSERVER_REQUEST_TABLE,
            "RELAY_SQLSERVER_RESULT_TABLE": settings.RELAY_SQLSERVER_RESULT_TABLE,
        }
        missing = [name for name, value in required.items() if not value]
        if not {"ins_id", "sn", "customer_code", "material_code"} <= set(settings.RELAY_SQLSERVER_SN_COLUMN_MAP):
            missing.append("RELAY_SQLSERVER_SN_COLUMN_MAP")
        return sorted(set(missing))

    def _connection_string(self) -> str:
        missing = self._missing()
        if missing:
            raise SapMiddlewareConfigurationError("SAP_MIDDLEWARE_NOT_CONFIGURED:" + ",".join(missing))
        return (
            f"DRIVER={{{settings.RELAY_SQLSERVER_DRIVER}}};SERVER={settings.RELAY_SQLSERVER_HOST},{settings.RELAY_SQLSERVER_PORT};"
            f"DATABASE={settings.RELAY_SQLSERVER_DATABASE};UID={settings.RELAY_SQLSERVER_USER};PWD={settings.RELAY_SQLSERVER_PASSWORD};"
            f"Encrypt={'yes' if settings.RELAY_SQLSERVER_ENCRYPT else 'no'};"
            f"TrustServerCertificate={'yes' if settings.RELAY_SQLSERVER_TRUST_SERVER_CERTIFICATE else 'no'};"
        )

    def _connect(self):
        try:
            import pyodbc
        except ImportError as exc:
            raise SapMiddlewareConfigurationError("RELAY_PYODBC_NOT_INSTALLED") from exc
        timeout_seconds = max(1, int(settings.RELAY_TIMEOUT_SECONDS))
        connection = pyodbc.connect(
            self._connection_string(),
            timeout=timeout_seconds,
            autocommit=False,
        )
        # ``connect(timeout=...)`` bounds login only. The connection timeout
        # also bounds INSERT/SELECT execution so a lock or gateway stall cannot
        # occupy one worker indefinitely.
        connection.timeout = timeout_seconds
        return connection

    @staticmethod
    def _columns(cursor: Any, schema: str, table: str) -> dict[str, tuple[str, int | None, str]]:
        rows = cursor.execute(
            "SELECT COLUMN_NAME,DATA_TYPE,CHARACTER_MAXIMUM_LENGTH,IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=? AND TABLE_NAME=?", schema, table,
        ).fetchall()
        return {str(r[0]).casefold(): (str(r[1]).casefold(), r[2], str(r[3]).upper()) for r in rows}

    @staticmethod
    def _has_unique(cursor: Any, schema: str, table: str, column: str) -> bool:
        return cursor.execute(
            "SELECT TOP (1) 1 FROM sys.indexes i JOIN sys.index_columns ic ON i.object_id=ic.object_id AND i.index_id=ic.index_id "
            "JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id JOIN sys.tables t ON t.object_id=i.object_id "
            "JOIN sys.schemas s ON s.schema_id=t.schema_id WHERE s.name=? AND t.name=? AND i.is_unique=1 AND c.name=?",
            schema, table, column,
        ).fetchone() is not None

    def _check_sync(self) -> ConnectionHealth:
        missing = self._missing()
        if missing:
            return ConnectionHealth(False, "misconfigured", tuple(missing), {"adapter": "sqlserver"})
        try:
            with closing(self._connect()) as connection:
                cursor = connection.cursor()
                request = self._columns(cursor, settings.RELAY_SQLSERVER_REQUEST_SCHEMA, settings.RELAY_SQLSERVER_REQUEST_TABLE)
                result = self._columns(cursor, settings.RELAY_SQLSERVER_RESULT_SCHEMA, settings.RELAY_SQLSERVER_RESULT_TABLE)
                request_key = settings.RELAY_SQLSERVER_REQUEST_ID_COLUMN.casefold()
                for label, columns, schema, table in (
                    ("RMA1", request, settings.RELAY_SQLSERVER_REQUEST_SCHEMA, settings.RELAY_SQLSERVER_REQUEST_TABLE),
                    ("RMA2", result, settings.RELAY_SQLSERVER_RESULT_SCHEMA, settings.RELAY_SQLSERVER_RESULT_TABLE),
                ):
                    actual = columns.get(request_key)
                    if actual != ("char", 36, "NO") or not self._has_unique(cursor, schema, table, settings.RELAY_SQLSERVER_REQUEST_ID_COLUMN):
                        raise SapSchemaMismatchError(f"{label}_REQUEST_ID_CONTRACT_MISMATCH:{actual}")
                for column in ("internalSN", "itemCode", "customer", "BPBillAddr", "BPCellular", "insID"):
                    if column.casefold() not in request:
                        raise SapSchemaMismatchError(f"RMA1_COLUMN_MISSING:{column}")
                for column in ("callID", "U_ModVersion"):
                    if request.get(column.casefold(), (None, None, "NO"))[2] != "YES":
                        raise SapSchemaMismatchError(f"RMA1_DATABASE_OWNED_FIELD_NOT_NULLABLE:{column}")
                if settings.RELAY_SQLSERVER_RMA_COLUMN.casefold() not in result:
                    raise SapSchemaMismatchError("RMA2_RMA_COLUMN_MISSING")
                call_id = settings.RELAY_SQLSERVER_CALL_ID_COLUMN.casefold()
                if call_id not in result or result[call_id][0] not in {"int", "bigint"}:
                    raise SapSchemaMismatchError("RMA2_CALL_ID_CONTRACT_MISMATCH")
                definitions = [str(r[0] or "") for r in cursor.execute(
                    "SELECT OBJECT_DEFINITION(tr.object_id) FROM sys.triggers tr JOIN sys.tables t ON t.object_id=tr.parent_id "
                    "JOIN sys.schemas s ON s.schema_id=t.schema_id WHERE s.name=? AND t.name=? AND tr.is_disabled=0",
                    settings.RELAY_SQLSERVER_REQUEST_SCHEMA, settings.RELAY_SQLSERVER_REQUEST_TABLE,
                ).fetchall()]
                insert_triggers = [v for v in definitions if "INSERT" in v.upper()]
                if any("requestid" not in v.casefold() for v in insert_triggers):
                    raise SapSchemaMismatchError("RMA1_GATEWAY_TRIGGER_NOT_REQUEST_ID")
        except SapSchemaMismatchError as exc:
            return ConnectionHealth(False, "schema_mismatch", details={"adapter": "sqlserver", "error": str(exc)})
        except Exception as exc:
            return ConnectionHealth(False, "unreachable", details={"adapter": "sqlserver", "error": type(exc).__name__})
        return ConnectionHealth(True, "configured", details={"adapter": "sqlserver"})

    async def check_connection(self) -> ConnectionHealth:
        return await asyncio.to_thread(self._check_sync)

    def _sn_select_contract(self) -> tuple[dict[str, str], str, str]:
        mapping = dict(settings.RELAY_SQLSERVER_SN_COLUMN_MAP or {})
        invalid = sorted(set(mapping) - _SN_LOCAL_FIELDS)
        if invalid:
            raise SapMiddlewareConfigurationError("RELAY_SN_LOCAL_FIELDS_INVALID:" + ",".join(invalid))
        remote_ins_id = mapping.get("ins_id")
        if not remote_ins_id:
            raise SapMiddlewareConfigurationError("RELAY_SN_INS_ID_COLUMN_MISSING")
        return (
            mapping,
            _qualified(settings.RELAY_SQLSERVER_SN_SCHEMA, settings.RELAY_SQLSERVER_SN_TABLE),
            _identifier(remote_ins_id),
        )

    def _inspect_sn_snapshot_sync(self) -> ExternalSnSnapshot:
        _mapping, table, ins_id = self._sn_select_contract()
        with closing(self._connect()) as connection:
            row = connection.cursor().execute(
                f"SELECT COUNT_BIG(1), COUNT_BIG({ins_id}), "
                f"COUNT_BIG(DISTINCT {ins_id}), MAX({ins_id}) FROM {table}"
            ).fetchone()
        source_count = int(row[0] or 0)
        non_null_count = int(row[1] or 0)
        distinct_count = int(row[2] or 0)
        return ExternalSnSnapshot(
            source_count=source_count,
            max_ins_id=int(row[3]) if row[3] is not None else None,
            duplicate_ins_id_count=max(0, non_null_count - distinct_count),
            null_ins_id_count=max(0, source_count - non_null_count),
        )

    async def inspect_sn_snapshot(self) -> ExternalSnSnapshot:
        return await asyncio.to_thread(self._inspect_sn_snapshot_sync)

    @staticmethod
    def _external_sn_record(
        row: Any, names: list[str], mapping: dict[str, str]
    ) -> ExternalSnRecord:
        raw = dict(zip(names, row, strict=True))
        values = {local: raw.get(remote) for local, remote in mapping.items()}
        return ExternalSnRecord(
            sn=str(values.get("sn") or "").strip().upper(),
            customer_code=str(values.get("customer_code") or "").strip(),
            customer_name=str(values.get("customer_name") or "").strip(),
            material_code=str(values.get("material_code") or "").strip(),
            ins_id=int(values["ins_id"]) if values.get("ins_id") is not None else None,
            material_name=str(values.get("material_name") or "").strip() or None,
            values=values,
            raw_data=raw,
        )

    def _fetch_sn_records_page_sync(
        self, *, after_ins_id: int | None, max_ins_id: int, limit: int
    ) -> list[ExternalSnRecord]:
        mapping, table, ins_id = self._sn_select_contract()
        columns = list(dict.fromkeys(mapping.values()))
        selected = ", ".join(_identifier(value) for value in columns)
        page_size = max(1, min(int(limit), 5000))
        with closing(self._connect()) as connection:
            cursor = connection.cursor()
            if after_ins_id is None:
                rows_cursor = cursor.execute(
                    f"SELECT TOP (?) {selected} FROM {table} "
                    f"WHERE {ins_id} <= ? ORDER BY {ins_id} ASC",
                    page_size,
                    int(max_ins_id),
                )
            else:
                rows_cursor = cursor.execute(
                    f"SELECT TOP (?) {selected} FROM {table} "
                    f"WHERE {ins_id} > ? AND {ins_id} <= ? ORDER BY {ins_id} ASC",
                    page_size,
                    int(after_ins_id),
                    int(max_ins_id),
                )
            names = [str(value[0]) for value in rows_cursor.description]
            rows = rows_cursor.fetchall()
        return [self._external_sn_record(row, names, mapping) for row in rows]

    async def fetch_sn_records_page(
        self, *, after_ins_id: int | None, max_ins_id: int, limit: int
    ) -> Sequence[ExternalSnRecord]:
        return await asyncio.to_thread(
            self._fetch_sn_records_page_sync,
            after_ins_id=after_ins_id,
            max_ins_id=max_ins_id,
            limit=limit,
        )

    def _submit_sync(self, items: Sequence[ExternalRmaSubmissionItem]) -> None:
        if not items: raise SapTransactionError("SAP_SUBMISSION_BATCH_EMPTY")
        columns = list(items[0].payload)
        if not columns or columns[0] != "RequestID" or any(list(v.payload) != columns for v in items):
            raise SapMiddlewareConfigurationError("RMA1_PARAMETER_CONTRACT_INCONSISTENT")
        target = _qualified(settings.RELAY_SQLSERVER_REQUEST_SCHEMA, settings.RELAY_SQLSERVER_REQUEST_TABLE)
        sql = f"INSERT INTO {target} ({', '.join(_identifier(v) for v in columns)}) VALUES ({', '.join('?' for _ in columns)})"
        connection = self._connect(); executed = False
        try:
            cursor = connection.cursor()
            for item in items:
                if str(item.request_id) != str(item.payload["RequestID"]): raise SapTransactionError("RMA1_REQUEST_ID_MISMATCH")
                cursor.execute(sql, [_payload_value(item.payload[v]) for v in columns]); executed = True
            connection.commit()
        except SapTransactionError:
            connection.rollback(); raise
        except Exception as exc:
            try: connection.rollback()
            except Exception:
                if executed: raise SapUnknownCommitStateError("SAP_SUBMIT_RESULT_UNKNOWN") from exc
            if executed and any(v in str(exc).casefold() for v in ("connection", "timeout", "08s01")):
                raise SapUnknownCommitStateError("SAP_SUBMIT_RESULT_UNKNOWN") from exc
            raise SapTransactionError("SAP_BATCH_SUBMIT_FAILED") from exc
        finally: connection.close()

    async def submit_rma_batch(self, items: Sequence[ExternalRmaSubmissionItem]) -> None:
        health = await self.check_connection()
        if not health.configured:
            raise SapSchemaMismatchError("SAP_MIDDLEWARE_PREFLIGHT_FAILED:" + str(health.details.get("error") or health.status))
        await asyncio.to_thread(self._submit_sync, items)

    def _query_ids(self, request_ids: Sequence[UUID], *, result: bool) -> list[Any]:
        if not request_ids: return []
        schema = settings.RELAY_SQLSERVER_RESULT_SCHEMA if result else settings.RELAY_SQLSERVER_REQUEST_SCHEMA
        table_name = settings.RELAY_SQLSERVER_RESULT_TABLE if result else settings.RELAY_SQLSERVER_REQUEST_TABLE
        table = _qualified(schema, table_name); key = _identifier(settings.RELAY_SQLSERVER_REQUEST_ID_COLUMN)
        found: list[Any] = []
        with closing(self._connect()) as connection:
            cursor = connection.cursor()
            for offset in range(0, len(request_ids), 1000):
                chunk = request_ids[offset:offset + 1000]; placeholders = ", ".join("?" for _ in chunk)
                if not result:
                    rows = cursor.execute(f"SELECT {key} FROM {table} WHERE {key} IN ({placeholders})", [str(v) for v in chunk]).fetchall()
                    found.extend(UUID(str(row[0])) for row in rows); continue
                rma = _identifier(settings.RELAY_SQLSERVER_RMA_COLUMN)
                call_id = _identifier(settings.RELAY_SQLSERVER_CALL_ID_COLUMN)
                rows = cursor.execute(
                    f"SELECT {key},[internalSN],{rma},[CREATEDATE],{call_id} FROM {table} WHERE {key} IN ({placeholders})",
                    [str(v) for v in chunk],
                ).fetchall()
                for row in rows:
                    found.append(ExternalRmaResult(
                        request_id=UUID(str(row[0])), sn=str(row[1]).strip() if row[1] is not None else None,
                        rma_no=str(row[2]).strip() if row[2] is not None else None,
                        remote_call_id=str(row[4]).strip() if row[4] is not None else None,
                        raw_data={"RequestID": str(row[0]), "internalSN": row[1], "U_CustomerNum": row[2], "CREATEDATE": row[3], "callID": row[4]},
                    ))
        return found

    def _find_sync(self, request_ids: Sequence[UUID]) -> list[ExternalRmaResult]:
        return self._query_ids(request_ids, result=True)

    async def find_submitted_request_ids(self, request_ids: Sequence[UUID]) -> Sequence[UUID]:
        return await asyncio.to_thread(self._query_ids, request_ids, result=False)

    async def query_rma_results(self, request_ids: Sequence[UUID]) -> Sequence[ExternalRmaResult]:
        return await asyncio.to_thread(self._query_ids, request_ids, result=True)
