from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from typing import Any, Sequence
from uuid import UUID

from app.config import settings
from app.integrations.sap_middleware.contracts import (
    ConnectionHealth,
    ExternalRmaResult,
    ExternalRmaSubmissionItem,
    ExternalSnRecord,
    SapMiddlewareConfigurationError,
    SapSchemaMismatchError,
    SapSnapshotUnstableError,
    SapTransactionError,
    SapUnknownCommitStateError,
)


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#@]*$")
_SN_LOCAL_FIELDS = {
    "sn",
    "customer_code",
    "customer_name",
    "material_code",
    "material_name",
    "service_tracking_card_no",
    "parent_sn",
    "top_sn",
    "parent_material_code",
    "top_material_code",
    "asset_status",
    "warranty_start_date",
    "warranty_end_date",
}


def _identifier(value: str) -> str:
    if not value or not _IDENTIFIER.fullmatch(value):
        raise SapMiddlewareConfigurationError(f"SAP_IDENTIFIER_INVALID:{value[:80]}")
    return f"[{value}]"


def _qualified(schema: str, name: str) -> str:
    return f"{_identifier(schema)}.{_identifier(name)}"


def _payload_value(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    if isinstance(current, (dict, list)):
        return json.dumps(current, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(current, (date, datetime)):
        return current.isoformat()
    return current


class SqlServerSapMiddlewareAdapter:
    def __init__(self, *, invalid_adapter: str | None = None):
        self.invalid_adapter = invalid_adapter

    def _missing(self) -> list[str]:
        if self.invalid_adapter:
            return [f"RELAY_ADAPTER_INVALID:{self.invalid_adapter}"]
        if not settings.RELAY_SQLSERVER_ENABLED:
            return ["RELAY_SQLSERVER_ENABLED"]
        values = {
            "RELAY_SQLSERVER_HOST": settings.RELAY_SQLSERVER_HOST,
            "RELAY_SQLSERVER_DATABASE": settings.RELAY_SQLSERVER_DATABASE,
            "RELAY_SQLSERVER_USER": settings.RELAY_SQLSERVER_USER,
            "RELAY_SQLSERVER_PASSWORD": settings.RELAY_SQLSERVER_PASSWORD,
            "RELAY_SQLSERVER_DRIVER": settings.RELAY_SQLSERVER_DRIVER,
            "RELAY_SQLSERVER_SN_TABLE": settings.RELAY_SQLSERVER_SN_TABLE,
            "RELAY_SQLSERVER_RESULT_TARGET": settings.RELAY_SQLSERVER_RESULT_TARGET,
            "RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN": settings.RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN,
            "RELAY_SQLSERVER_RMA_COLUMN": settings.RELAY_SQLSERVER_RMA_COLUMN,
        }
        missing = [name for name, value in values.items() if not value]
        if not settings.RELAY_SQLSERVER_RESULT_COLUMN_MAP:
            missing.append("RELAY_SQLSERVER_RESULT_COLUMN_MAP")
        required_sn = {"sn", "customer_code", "material_code"}
        sn_mapping = dict(settings.RELAY_SQLSERVER_SN_COLUMN_MAP or {})
        if not required_sn <= set(sn_mapping):
            missing.append("RELAY_SQLSERVER_SN_COLUMN_MAP")
        if settings.RELAY_SQLSERVER_RESULT_MODE != "table":
            missing.append("RELAY_SQLSERVER_RESULT_MODE_MUST_BE_TABLE")
        return sorted(set(missing))

    def _connection_string(self) -> str:
        missing = self._missing()
        if missing:
            raise SapMiddlewareConfigurationError("SAP_MIDDLEWARE_NOT_CONFIGURED:" + ",".join(missing))
        encrypt = "yes" if settings.RELAY_SQLSERVER_ENCRYPT else "no"
        trust = "yes" if settings.RELAY_SQLSERVER_TRUST_SERVER_CERTIFICATE else "no"
        return (
            f"DRIVER={{{settings.RELAY_SQLSERVER_DRIVER}}};"
            f"SERVER={settings.RELAY_SQLSERVER_HOST},{settings.RELAY_SQLSERVER_PORT};"
            f"DATABASE={settings.RELAY_SQLSERVER_DATABASE};"
            f"UID={settings.RELAY_SQLSERVER_USER};PWD={settings.RELAY_SQLSERVER_PASSWORD};"
            f"Encrypt={encrypt};TrustServerCertificate={trust};"
        )

    def _connect(self):
        try:
            import pyodbc
        except ImportError as exc:
            raise SapMiddlewareConfigurationError("RELAY_PYODBC_NOT_INSTALLED") from exc
        return pyodbc.connect(
            self._connection_string(),
            timeout=max(1, int(settings.RELAY_TIMEOUT_SECONDS)),
            autocommit=False,
        )

    def _check_sync(self) -> ConnectionHealth:
        missing = self._missing()
        if missing:
            return ConnectionHealth(False, "misconfigured", tuple(missing), {"adapter": "sqlserver"})
        target_schema = settings.RELAY_SQLSERVER_RESULT_SCHEMA
        target_name = settings.RELAY_SQLSERVER_RESULT_TARGET
        required = {
            settings.RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN.casefold(): "uniqueidentifier",
            settings.RELAY_SQLSERVER_RMA_COLUMN.casefold(): None,
        }
        required.update(
            {str(column).casefold(): None for column in settings.RELAY_SQLSERVER_RESULT_COLUMN_MAP.values()}
        )
        try:
            with self._connect() as connection:
                cursor = connection.cursor()
                cursor.execute("SELECT 1").fetchone()
                columns = cursor.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
                    target_schema,
                    target_name,
                ).fetchall()
                found = {str(row[0]).casefold(): (str(row[1]).casefold(), str(row[2]).upper()) for row in columns}
                absent = sorted(name for name in required if name not in found)
                source = found.get(settings.RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN.casefold())
                if absent or source is None or source[0] != "uniqueidentifier" or source[1] != "NO":
                    raise SapSchemaMismatchError(
                        "SAP_RESULT_SCHEMA_MISMATCH:"
                        + json.dumps({"missing": absent, "source_request": source}, ensure_ascii=False)
                    )
                unique_row = cursor.execute(
                    "SELECT TOP (1) 1 FROM sys.indexes i "
                    "JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id "
                    "JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id "
                    "JOIN sys.tables t ON i.object_id = t.object_id "
                    "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                    "WHERE s.name = ? AND t.name = ? AND i.is_unique = 1 AND ic.key_ordinal > 0 "
                    "GROUP BY i.object_id, i.index_id "
                    "HAVING COUNT(*) = 1 AND MAX(CASE WHEN c.name = ? THEN 1 ELSE 0 END) = 1",
                    target_schema,
                    target_name,
                    settings.RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN,
                ).fetchone()
                if unique_row is None:
                    raise SapSchemaMismatchError("SAP_SOURCE_REQUEST_ID_UNIQUE_INDEX_MISSING")
        except SapSchemaMismatchError as exc:
            return ConnectionHealth(
                False,
                "schema_mismatch",
                details={"adapter": "sqlserver", "error": str(exc)},
            )
        except Exception as exc:
            return ConnectionHealth(False, "unreachable", details={"adapter": "sqlserver", "error": type(exc).__name__})
        return ConnectionHealth(True, "configured", details={"adapter": "sqlserver"})

    async def check_connection(self) -> ConnectionHealth:
        return await asyncio.to_thread(self._check_sync)

    def _sn_mapping(self) -> tuple[dict[str, str], list[str]]:
        mapping = dict(settings.RELAY_SQLSERVER_SN_COLUMN_MAP or {})
        invalid = sorted(set(mapping) - _SN_LOCAL_FIELDS)
        if invalid:
            raise SapMiddlewareConfigurationError("RELAY_SN_LOCAL_FIELDS_INVALID:" + ",".join(invalid))
        columns = list(dict.fromkeys(mapping.values()))
        for column in columns:
            _identifier(column)
        return mapping, columns

    def _fetch_all_sn_sync(self) -> list[ExternalSnRecord]:
        mapping, columns = self._sn_mapping()
        table = _qualified(settings.RELAY_SQLSERVER_SN_SCHEMA, settings.RELAY_SQLSERVER_SN_TABLE)
        selected = ", ".join(_identifier(column) for column in columns)
        records: list[ExternalSnRecord] = []
        with self._connect() as connection:
            cursor = connection.cursor()
            count_before = int(cursor.execute(f"SELECT COUNT_BIG(1) FROM {table}").fetchone()[0])
            row_cursor = cursor.execute(f"SELECT {selected} FROM {table}")
            names = [str(item[0]) for item in row_cursor.description]
            while True:
                rows = row_cursor.fetchmany(max(1, min(settings.RELAY_SQLSERVER_BATCH_SIZE, 5000)))
                if not rows:
                    break
                for row in rows:
                    raw = dict(zip(names, row, strict=True))
                    values = {local: raw.get(remote) for local, remote in mapping.items()}
                    records.append(
                        ExternalSnRecord(
                            sn=str(values.get("sn") or "").strip().upper(),
                            customer_code=str(values.get("customer_code") or "").strip(),
                            customer_name=str(values.get("customer_name") or "").strip(),
                            material_code=str(values.get("material_code") or "").strip(),
                            material_name=str(values.get("material_name") or "").strip() or None,
                            values=values,
                            raw_data=raw,
                        )
                    )
            count_after = int(cursor.execute(f"SELECT COUNT_BIG(1) FROM {table}").fetchone()[0])
        if count_before != count_after or count_after != len(records):
            raise SapSnapshotUnstableError(
                f"SAP_SN_SNAPSHOT_UNSTABLE:{count_before}:{len(records)}:{count_after}"
            )
        return records

    async def fetch_all_sn_records(self) -> Sequence[ExternalSnRecord]:
        return await asyncio.to_thread(self._fetch_all_sn_sync)

    def _submit_sync(self, items: Sequence[ExternalRmaSubmissionItem]) -> None:
        if not items:
            raise SapTransactionError("SAP_SUBMISSION_BATCH_EMPTY")
        mapping = dict(settings.RELAY_SQLSERVER_RESULT_COLUMN_MAP or {})
        target = _qualified(settings.RELAY_SQLSERVER_RESULT_SCHEMA, settings.RELAY_SQLSERVER_RESULT_TARGET)
        source_column = settings.RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN
        if source_column in mapping.values():
            raise SapMiddlewareConfigurationError("SAP_SOURCE_REQUEST_ID_MAPPING_DUPLICATED")
        columns = [source_column, *mapping.values()]
        for column in columns:
            _identifier(column)
        sql = (
            f"INSERT INTO {target} ({', '.join(_identifier(column) for column in columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})"
        )
        connection = self._connect()
        executed = False
        try:
            cursor = connection.cursor()
            for item in items:
                values = [str(item.source_request_id), *[_payload_value(item.payload, path) for path in mapping]]
                cursor.execute(sql, values)
                executed = True
            connection.commit()
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:
                if executed:
                    raise SapUnknownCommitStateError("SAP_SUBMIT_RESULT_UNKNOWN") from exc
            message = str(exc).casefold()
            if executed and any(token in message for token in ("communication link", "connection", "timeout", "08s01")):
                raise SapUnknownCommitStateError("SAP_SUBMIT_RESULT_UNKNOWN") from exc
            raise SapTransactionError("SAP_BATCH_SUBMIT_FAILED") from exc
        finally:
            connection.close()

    async def submit_rma_batch(self, items: Sequence[ExternalRmaSubmissionItem]) -> None:
        health = await self.check_connection()
        if not health.configured:
            raise SapSchemaMismatchError(
                "SAP_MIDDLEWARE_PREFLIGHT_FAILED:" + str(health.details.get("error") or health.status)
            )
        await asyncio.to_thread(self._submit_sync, items)

    def _find_sync(self, source_request_ids: Sequence[UUID]) -> list[ExternalRmaResult]:
        if not source_request_ids:
            return []
        target = _qualified(settings.RELAY_SQLSERVER_RESULT_SCHEMA, settings.RELAY_SQLSERVER_RESULT_TARGET)
        source_column = _identifier(settings.RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN)
        rma_column = _identifier(settings.RELAY_SQLSERVER_RMA_COLUMN)
        sn_remote = (settings.RELAY_SQLSERVER_RESULT_COLUMN_MAP or {}).get("sn")
        sn_select = _identifier(sn_remote) if sn_remote else "NULL"
        results: list[ExternalRmaResult] = []
        with self._connect() as connection:
            cursor = connection.cursor()
            for offset in range(0, len(source_request_ids), 1000):
                chunk = source_request_ids[offset : offset + 1000]
                placeholders = ", ".join("?" for _ in chunk)
                rows = cursor.execute(
                    f"SELECT {source_column}, {sn_select}, {rma_column} FROM {target} "
                    f"WHERE {source_column} IN ({placeholders})",
                    [str(value) for value in chunk],
                ).fetchall()
                for row in rows:
                    results.append(
                        ExternalRmaResult(
                            source_request_id=UUID(str(row[0])),
                            sn=str(row[1]).strip() if row[1] is not None else None,
                            rma_no=str(row[2]).strip() if row[2] is not None else None,
                            raw_data={
                                "source_request_id": str(row[0]),
                                "sn": row[1],
                                "rma_no": row[2],
                            },
                        )
                    )
        return results

    async def find_records_by_source_request_ids(
        self, source_request_ids: Sequence[UUID]
    ) -> Sequence[ExternalRmaResult]:
        return await asyncio.to_thread(self._find_sync, source_request_ids)
