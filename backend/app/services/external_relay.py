from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ExternalSyncCheckpoint, SnAsset
from app.services.common import utcnow


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
    if settings.RELAY_SQLSERVER_RESULT_MODE == "table" and not settings.RELAY_SQLSERVER_RESULT_UNIQUE_COLUMN:
        missing.append("RELAY_SQLSERVER_RESULT_UNIQUE_COLUMN")
    if settings.RELAY_SQLSERVER_RESULT_MODE not in {"table", "stored_procedure"}:
        missing.append("RELAY_SQLSERVER_RESULT_MODE")
    return {
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
    with _connect() as connection:
        cursor = connection.cursor()
        if settings.RELAY_SQLSERVER_RESULT_MODE == "stored_procedure":
            placeholders = ", ".join("?" for _ in values)
            cursor.execute(f"{{CALL {target} ({placeholders})}}", values)
            remote_key = str(_payload_value(payload, settings.RELAY_SQLSERVER_RESULT_UNIQUE_PAYLOAD_KEY) or "")
        else:
            unique_column = _identifier(settings.RELAY_SQLSERVER_RESULT_UNIQUE_COLUMN)
            unique_value = _payload_value(payload, settings.RELAY_SQLSERVER_RESULT_UNIQUE_PAYLOAD_KEY)
            existing = cursor.execute(f"SELECT TOP (1) {unique_column} FROM {target} WHERE {unique_column} = ?", unique_value).fetchone()
            if existing is not None:
                return {"status": "succeeded", "remote_record_key": str(existing[0]), "idempotent_reuse": True}
            columns = ", ".join(_identifier(column) for column in mapping.values())
            placeholders = ", ".join("?" for _ in values)
            cursor.execute(f"INSERT INTO {target} ({columns}) VALUES ({placeholders})", values)
            remote_key = str(unique_value)
        connection.commit()
        return {"status": "succeeded", "remote_record_key": remote_key, "idempotent_reuse": False}


async def validate_sn_against_relay(sn: str) -> dict[str, Any]:
    if not settings.RELAY_SQLSERVER_ENABLED:
        return {"status": "disabled", "sn": sn, "record": None}
    if not relay_configured():
        return {"status": "misconfigured", "sn": sn, "record": None}
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
    try:
        return await asyncio.to_thread(_write_ticket_snapshot, payload)
    except Exception as exc:
        raise RelayOperationError("RELAY_TICKET_EXPORT_FAILED") from exc


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
