from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ExternalSyncCheckpoint, SnAsset
from app.services.common import utcnow
from app.services.mail_safety import test_mail_configuration_reasons


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#@]*$")
_SN_LOCAL_FIELDS = {
    "sn", "customer_code", "customer_name", "material_code", "material_name",
    "service_tracking_card_no", "parent_sn", "top_sn", "parent_material_code", "top_material_code",
    "asset_status", "warranty_start_date", "warranty_end_date",
}


class RelayConfigurationError(RuntimeError):
    pass


class RelayOperationError(RuntimeError):
    pass


class RelaySubmissionUncertainError(RelayOperationError):
    pass


def _identifier(value: str) -> str:
    if not value or not _IDENTIFIER.fullmatch(value):
        raise RelayConfigurationError(f"RELAY_IDENTIFIER_INVALID:{value[:80]}")
    return f"[{value}]"


def _qualified(schema: str, name: str) -> str:
    return f"{_identifier(schema)}.{_identifier(name)}"


def _base_missing() -> list[str]:
    values = {
        "RELAY_SQLSERVER_HOST": settings.RELAY_SQLSERVER_HOST,
        "RELAY_SQLSERVER_DATABASE": settings.RELAY_SQLSERVER_DATABASE,
        "RELAY_SQLSERVER_USER": settings.RELAY_SQLSERVER_USER,
        "RELAY_SQLSERVER_PASSWORD": settings.RELAY_SQLSERVER_PASSWORD,
        "RELAY_SQLSERVER_DRIVER": settings.RELAY_SQLSERVER_DRIVER,
    }
    return [name for name, value in values.items() if not value]


def relay_configuration_status() -> dict[str, Any]:
    if not settings.RELAY_SQLSERVER_ENABLED:
        return {"status": "disabled", "configured": False, "missing": []}
    adapter = settings.RELAY_ADAPTER.strip().lower()
    if adapter == "test_http":
        missing: list[str] = []
        parsed = urlsplit(settings.TEST_RELAY_BASE_URL)
        if settings.APP_ENV.lower() not in {"dev", "test"}:
            missing.append("TEST_RELAY_ENV_NOT_ALLOWED")
        if not settings.RUN_REAL_MAIL_INTEGRATION_TESTS:
            missing.append("RUN_REAL_MAIL_INTEGRATION_TESTS")
        if test_mail_configuration_reasons():
            missing.append("TEST_MAIL_GATE_FAILED")
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            missing.append("TEST_RELAY_LOOPBACK_URL_REQUIRED")
        if not settings.TEST_RELAY_TOKEN:
            missing.append("TEST_RELAY_TOKEN")
        return {
            "adapter": adapter,
            "status": "misconfigured" if missing else "configured",
            "configured": not missing,
            "missing": sorted(set(missing)),
        }
    if adapter != "sqlserver":
        return {
            "adapter": adapter,
            "status": "misconfigured",
            "configured": False,
            "missing": ["RELAY_ADAPTER_INVALID"],
        }
    missing = _base_missing()
    if not settings.RELAY_SQLSERVER_SN_TABLE:
        missing.append("RELAY_SQLSERVER_SN_TABLE")
    if not settings.RELAY_SQLSERVER_SN_PRIMARY_KEY:
        missing.append("RELAY_SQLSERVER_SN_PRIMARY_KEY")
    if not settings.RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN:
        missing.append("RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN")
    if not settings.RELAY_SQLSERVER_RESULT_TARGET:
        missing.append("RELAY_SQLSERVER_RESULT_TARGET")
    if not settings.RELAY_SQLSERVER_RESULT_COLUMN_MAP:
        missing.append("RELAY_SQLSERVER_RESULT_COLUMN_MAP")
    if not settings.RELAY_SQLSERVER_CALL_ID_COLUMN:
        missing.append("RELAY_SQLSERVER_CALL_ID_COLUMN")
    if not settings.RELAY_SQLSERVER_RMA_COLUMN:
        missing.append("RELAY_SQLSERVER_RMA_COLUMN")
    if settings.RELAY_SQLSERVER_RESULT_MODE not in {"table", "stored_procedure"}:
        missing.append("RELAY_SQLSERVER_RESULT_MODE")
    return {
        "adapter": adapter,
        "status": "misconfigured" if missing else "configured",
        "configured": not missing,
        "missing": sorted(set(missing)),
    }


def relay_configured() -> bool:
    return relay_configuration_status()["configured"]


def _connection_string() -> str:
    status = relay_configuration_status()
    if not status["configured"]:
        raise RelayConfigurationError("RELAY_SQLSERVER_NOT_CONFIGURED:" + ",".join(status["missing"]))
    encrypt = "yes" if settings.RELAY_SQLSERVER_ENCRYPT else "no"
    trust = "yes" if settings.RELAY_SQLSERVER_TRUST_SERVER_CERTIFICATE else "no"
    return (
        f"DRIVER={{{settings.RELAY_SQLSERVER_DRIVER}}};"
        f"SERVER={settings.RELAY_SQLSERVER_HOST},{settings.RELAY_SQLSERVER_PORT};"
        f"DATABASE={settings.RELAY_SQLSERVER_DATABASE};"
        f"UID={settings.RELAY_SQLSERVER_USER};PWD={settings.RELAY_SQLSERVER_PASSWORD};"
        f"Encrypt={encrypt};TrustServerCertificate={trust};"
    )


def _connect():
    try:
        import pyodbc
    except ImportError as exc:
        raise RelayConfigurationError("RELAY_PYODBC_NOT_INSTALLED") from exc
    return pyodbc.connect(_connection_string(), timeout=max(1, int(settings.RELAY_TIMEOUT_SECONDS)))


def _sn_columns() -> tuple[dict[str, str], list[str]]:
    mapping = dict(settings.RELAY_SQLSERVER_SN_COLUMN_MAP or {})
    invalid = sorted(set(mapping) - _SN_LOCAL_FIELDS)
    if invalid:
        raise RelayConfigurationError("RELAY_SN_LOCAL_FIELDS_INVALID:" + ",".join(invalid))
    required = {"sn", "customer_code", "customer_name", "material_code", "asset_status"}
    if not required <= set(mapping):
        raise RelayConfigurationError("RELAY_SN_REQUIRED_MAPPING_MISSING:" + ",".join(sorted(required - set(mapping))))
    remote_columns = list(dict.fromkeys([*mapping.values(), settings.RELAY_SQLSERVER_SN_PRIMARY_KEY, settings.RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN]))
    for column in remote_columns:
        _identifier(column)
    return mapping, remote_columns


def _fetch_sn_rows(*, cursor: dict[str, Any] | None, full: bool) -> list[dict[str, Any]]:
    mapping, remote_columns = _sn_columns()
    del mapping
    table = _qualified(settings.RELAY_SQLSERVER_SN_SCHEMA, settings.RELAY_SQLSERVER_SN_TABLE)
    pk = _identifier(settings.RELAY_SQLSERVER_SN_PRIMARY_KEY)
    updated = _identifier(settings.RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN)
    select_columns = ", ".join(_identifier(column) for column in remote_columns)
    sql = f"SELECT TOP (?) {select_columns} FROM {table}"
    params: list[Any] = [max(1, min(settings.RELAY_SQLSERVER_BATCH_SIZE, 5000))]
    if not full and cursor and cursor.get("updated_at") is not None:
        sql += f" WHERE ({updated} > ?) OR ({updated} = ? AND {pk} > ?)"
        params.extend([cursor["updated_at"], cursor["updated_at"], cursor.get("primary_key")])
    sql += f" ORDER BY {updated}, {pk}"
    with _connect() as connection:
        row_cursor = connection.cursor()
        rows = row_cursor.execute(sql, params).fetchall()
        columns = [item[0] for item in row_cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in rows]


def _fetch_sn_exact(sn: str) -> dict[str, Any] | None:
    mapping, remote_columns = _sn_columns()
    table = _qualified(settings.RELAY_SQLSERVER_SN_SCHEMA, settings.RELAY_SQLSERVER_SN_TABLE)
    sn_column = _identifier(mapping["sn"])
    select_columns = ", ".join(_identifier(column) for column in remote_columns)
    with _connect() as connection:
        cursor = connection.cursor()
        row = cursor.execute(f"SELECT TOP (1) {select_columns} FROM {table} WHERE {sn_column} = ?", sn).fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        return dict(zip(columns, row, strict=True))


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


def _write_ticket_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    mapping = dict(settings.RELAY_SQLSERVER_RESULT_COLUMN_MAP or {})
    if not mapping:
        raise RelayConfigurationError("RELAY_RESULT_MAPPING_EMPTY")
    for column in mapping.values():
        _identifier(column)
    values = [_payload_value(payload, path) for path in mapping]
    target = _qualified(settings.RELAY_SQLSERVER_RESULT_SCHEMA, settings.RELAY_SQLSERVER_RESULT_TARGET)
    call_id_column = _identifier(settings.RELAY_SQLSERVER_CALL_ID_COLUMN)
    with _connect() as connection:
        cursor = connection.cursor()
        if settings.RELAY_SQLSERVER_RESULT_MODE == "stored_procedure":
            placeholders = ", ".join("?" for _ in values)
            cursor.execute(f"{{CALL {target} ({placeholders})}}", values)
            row = cursor.fetchone() if cursor.description else None
            remote_key = str(row[0]) if row and row[0] is not None else ""
            if not remote_key:
                raise RelayOperationError("RELAY_CALL_ID_NOT_RETURNED")
        else:
            columns = ", ".join(_identifier(column) for column in mapping.values())
            placeholders = ", ".join("?" for _ in values)
            # Once execute is invoked, a transport/driver exception cannot tell
            # us whether SQL Server committed the row.  CallID is the only
            # authoritative remote identity, so this becomes an explicit
            # uncertain/manual-reconciliation state and must not be reinserted.
            executed = False
            try:
                executed = True
                cursor.execute(
                    f"INSERT INTO {target} ({columns}) "
                    f"OUTPUT INSERTED.{call_id_column} VALUES ({placeholders})",
                    values,
                )
                row = cursor.fetchone()
                if row is None or row[0] is None:
                    raise RelayOperationError("RELAY_CALL_ID_NOT_RETURNED")
                remote_key = str(row[0])
                connection.commit()
            except RelayOperationError:
                raise
            except Exception as exc:
                if executed or getattr(cursor, "rowcount", -1) != -1:
                    raise RelaySubmissionUncertainError(
                        "RELAY_SUBMIT_RESULT_UNCERTAIN"
                    ) from exc
                raise
            return {
                "status": "succeeded",
                "remote_record_key": remote_key,
                "idempotent_reuse": False,
            }
        connection.commit()
        return {"status": "succeeded", "remote_record_key": remote_key, "idempotent_reuse": False}


def _fetch_rma_result(remote_call_id: str) -> dict[str, Any] | None:
    target = _qualified(settings.RELAY_SQLSERVER_RESULT_SCHEMA, settings.RELAY_SQLSERVER_RESULT_TARGET)
    call_id_column = _identifier(settings.RELAY_SQLSERVER_CALL_ID_COLUMN)
    rma_column = _identifier(settings.RELAY_SQLSERVER_RMA_COLUMN)
    with _connect() as connection:
        cursor = connection.cursor()
        row = cursor.execute(
            f"SELECT TOP (1) {call_id_column}, {rma_column} "
            f"FROM {target} WHERE {call_id_column} = ?",
            remote_call_id,
        ).fetchone()
        if row is None:
            return None
        return {"remote_call_id": str(row[0]), "rma_no": str(row[1]).strip() if row[1] is not None else None}


async def validate_sn_against_relay(sn: str) -> dict[str, Any]:
    if not settings.RELAY_SQLSERVER_ENABLED:
        return {"status": "disabled", "sn": sn, "record": None}
    if not relay_configured():
        return {"status": "misconfigured", "sn": sn, "record": None}
    if settings.RELAY_ADAPTER.strip().lower() == "test_http":
        return {"status": "disabled", "sn": sn, "record": None, "reason": "TEST_RELAY_HAS_NO_SN_MASTER"}
    try:
        record = await asyncio.to_thread(_fetch_sn_exact, sn)
    except Exception as exc:
        raise RelayOperationError("RELAY_SN_LOOKUP_FAILED") from exc
    return {"status": "found" if record else "not_found", "sn": sn, "record": record}


async def sync_sn_assets_from_relay(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    full: bool = False,
) -> dict[str, Any]:
    del user_id
    if not settings.RELAY_SQLSERVER_ENABLED:
        return {"status": "disabled", "configured": False, "synced_count": 0}
    if settings.RELAY_ADAPTER.strip().lower() == "test_http":
        return {
            "status": "disabled",
            "configured": True,
            "synced_count": 0,
            "reason": "TEST_RELAY_HAS_NO_SN_MASTER",
        }
    config = relay_configuration_status()
    if not config["configured"]:
        return {"status": "misconfigured", "configured": False, "missing": config["missing"], "synced_count": 0}
    checkpoint = await session.scalar(select(ExternalSyncCheckpoint).where(ExternalSyncCheckpoint.sync_name == "sqlserver_sn_assets"))
    if checkpoint is None:
        checkpoint = ExternalSyncCheckpoint(sync_name="sqlserver_sn_assets")
        session.add(checkpoint)
        await session.flush()
    cursor_value = json.loads(checkpoint.cursor_value) if checkpoint.cursor_value else None
    try:
        rows = await asyncio.to_thread(_fetch_sn_rows, cursor=cursor_value, full=full)
        mapping, _ = _sn_columns()
        seen_external_ids: set[str] = set()
        for raw in rows:
            values = {local: raw.get(remote) for local, remote in mapping.items()}
            sn = str(values.get("sn") or "").strip().upper()
            if not sn:
                continue
            external_id = str(raw.get(settings.RELAY_SQLSERVER_SN_PRIMARY_KEY) or "")
            seen_external_ids.add(external_id)
            asset = await session.scalar(select(SnAsset).where(SnAsset.sn == sn))
            if asset is None:
                asset = SnAsset(
                    sn=sn,
                    customer_code=str(values.get("customer_code") or ""),
                    customer_name=str(values.get("customer_name") or ""),
                    material_code=str(values.get("material_code") or ""),
                )
                session.add(asset)
            for field in _SN_LOCAL_FIELDS - {"sn"}:
                if field in values:
                    setattr(asset, field, values[field])
            asset.source_system = "sqlserver"
            asset.external_id = external_id or None
            asset.source_updated_at = raw.get(settings.RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN)
            asset.raw_data = {"sqlserver": {key: str(value) if isinstance(value, (date, datetime)) else value for key, value in raw.items()}}
        if full and seen_external_ids:
            await session.execute(
                update(SnAsset)
                .where(SnAsset.source_system == "sqlserver", SnAsset.external_id.not_in(seen_external_ids))
                .values(asset_status="invalid")
            )
            checkpoint.last_full_sync_at = utcnow()
        if rows:
            last = rows[-1]
            checkpoint.cursor_value = json.dumps({
                "updated_at": str(last.get(settings.RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN)),
                "primary_key": last.get(settings.RELAY_SQLSERVER_SN_PRIMARY_KEY),
            }, ensure_ascii=False, default=str)
        checkpoint.last_status = "succeeded"
        checkpoint.last_success_at = utcnow()
        checkpoint.last_error_code = None
        checkpoint.statistics_json = {"rows": len(rows), "full": full}
        return {"status": "succeeded", "configured": True, "synced_count": len(rows), "full": full}
    except Exception as exc:
        checkpoint.last_status = "failed"
        checkpoint.last_error_code = "RELAY_SN_SYNC_FAILED"
        raise RelayOperationError("RELAY_SN_SYNC_FAILED") from exc


async def push_ticket_snapshot_to_relay(payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.RELAY_SQLSERVER_ENABLED:
        return {"status": "disabled", "configured": False}
    config = relay_configuration_status()
    if not config["configured"]:
        return {"status": "misconfigured", "configured": False, "missing": config["missing"]}
    if settings.RELAY_ADAPTER.strip().lower() == "test_http":
        try:
            async with httpx.AsyncClient(timeout=settings.RELAY_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{settings.TEST_RELAY_BASE_URL.rstrip('/')}/records",
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.TEST_RELAY_TOKEN}"},
                )
                response.raise_for_status()
                result = response.json()
            if not result.get("remote_record_key"):
                raise RelayOperationError("RELAY_CALL_ID_NOT_RETURNED")
            return result
        except RelayOperationError:
            raise
        except Exception as exc:
            raise RelayOperationError("RELAY_TICKET_EXPORT_FAILED") from exc
    try:
        return await asyncio.to_thread(_write_ticket_snapshot, payload)
    except RelaySubmissionUncertainError:
        raise
    except Exception as exc:
        raise RelayOperationError("RELAY_TICKET_EXPORT_FAILED") from exc


async def poll_rma_from_relay(remote_call_id: str) -> dict[str, Any]:
    if not settings.RELAY_SQLSERVER_ENABLED:
        return {"status": "disabled", "remote_call_id": remote_call_id}
    config = relay_configuration_status()
    if not config["configured"]:
        return {
            "status": "misconfigured",
            "remote_call_id": remote_call_id,
            "missing": config["missing"],
        }
    if settings.RELAY_ADAPTER.strip().lower() == "test_http":
        try:
            async with httpx.AsyncClient(timeout=settings.RELAY_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{settings.TEST_RELAY_BASE_URL.rstrip('/')}/records/{remote_call_id}",
                    headers={"Authorization": f"Bearer {settings.TEST_RELAY_TOKEN}"},
                )
                if response.status_code == 404:
                    return {"status": "not_found", "remote_call_id": remote_call_id, "rma_no": None}
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            raise RelayOperationError("RELAY_RMA_POLL_FAILED") from exc
    try:
        result = await asyncio.to_thread(_fetch_rma_result, remote_call_id)
    except Exception as exc:
        raise RelayOperationError("RELAY_RMA_POLL_FAILED") from exc
    if result is None:
        return {"status": "not_found", "remote_call_id": remote_call_id, "rma_no": None}
    return {
        "status": "rma_received" if result.get("rma_no") else "waiting_rma",
        **result,
    }


async def push_ai_parse_result_to_relay(
    session: AsyncSession,
    *,
    parse_result_id: int,
    user_id: int | None = None,
) -> dict[str, Any]:
    del session, user_id
    # Kept only for compatibility. Validated ticket snapshots, not raw AI
    # candidates, are the supported SQL Server export contract.
    return {
        "status": "deprecated",
        "parse_result_id": parse_result_id,
        "message": "Use validated ticket relay export",
    }
