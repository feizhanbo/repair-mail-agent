from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import SystemConfig

CONFIG_KEYS = {
    "auto_send_enabled",
    "auto_followup_enabled",
    "rma_auto_send_enabled",
    "auto_apply_min_confidence",
    "auto_send_min_confidence",
    "confidence_threshold",
    "max_follow_up",
    "relay_sqlserver_enabled",
    "relay_sn_sync_enabled",
    "sn_schema",
    "sn_table",
    "sn_primary_key",
    "sn_updated_at_column",
    "sn_column_map",
    "batch_size",
    "snapshot_max_age_hours",
    "imap_fetch_enabled",
    "imap_poll_interval_minutes",
    "imap_folder",
    "imap_fetch_limit",
    "imap_unseen_only",
    "imap_max_retries",
    "imap_archive_to_oss",
}

DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "auto_send_enabled": False,
    "auto_followup_enabled": False,
    "rma_auto_send_enabled": True,
    "auto_apply_min_confidence": 0.85,
    "auto_send_min_confidence": 0.85,
    "confidence_threshold": 0.7,
    "max_follow_up": 3,
    "relay_sqlserver_enabled": False,
    "relay_sn_sync_enabled": False,
    "sn_schema": "dbo",
    "sn_table": "",
    "sn_primary_key": "",
    "sn_updated_at_column": "",
    "sn_column_map": {},
    "batch_size": 500,
    "snapshot_max_age_hours": 36,
    "imap_fetch_enabled": False,
    "imap_poll_interval_minutes": 5,
    "imap_folder": "INBOX",
    "imap_fetch_limit": 10,
    "imap_unseen_only": True,
    "imap_max_retries": 3,
    "imap_archive_to_oss": True,
}

CONFIG_GROUPS = {
    "business_automation": {
        "auto_send_enabled", "auto_followup_enabled", "rma_auto_send_enabled",
        "auto_apply_min_confidence", "auto_send_min_confidence", "confidence_threshold", "max_follow_up",
    },
    "sn_sync": {
        "relay_sqlserver_enabled", "relay_sn_sync_enabled", "sn_schema", "sn_table",
        "sn_primary_key", "sn_updated_at_column", "sn_column_map", "batch_size", "snapshot_max_age_hours",
    },
    "mail_fetch": {
        "imap_fetch_enabled", "imap_poll_interval_minutes", "imap_folder", "imap_fetch_limit",
        "imap_unseen_only", "imap_max_retries", "imap_archive_to_oss",
    },
}

_runtime_cache: dict[str, Any] | None = None


def _path() -> Path:
    return Path(settings.RUNTIME_CONFIG_PATH)


def _coerce_config(values: dict[str, Any]) -> dict[str, Any]:
    legacy_auto_send = values.get("reply_send_mode") == "auto_send"
    legacy_rma_disabled = values.get("rma_authorization_enabled") is False
    merged = {
        **DEFAULT_RUNTIME_CONFIG,
        "auto_send_enabled": bool(settings.AUTO_SEND_ENABLED),
        "auto_followup_enabled": bool(settings.AUTO_FOLLOWUP_ENABLED),
        "rma_auto_send_enabled": bool(settings.RMA_AUTO_SEND_ENABLED),
        "auto_apply_min_confidence": float(settings.AUTO_APPLY_MIN_CONFIDENCE),
        "auto_send_min_confidence": float(settings.AUTO_SEND_MIN_CONFIDENCE),
        "confidence_threshold": float(settings.CONFIDENCE_THRESHOLD),
        "max_follow_up": int(settings.MAX_FOLLOW_UP),
        "relay_sqlserver_enabled": bool(settings.RELAY_SQLSERVER_ENABLED),
        "relay_sn_sync_enabled": bool(settings.RELAY_SN_SYNC_ENABLED),
        "sn_schema": settings.RELAY_SQLSERVER_SN_SCHEMA,
        "sn_table": settings.RELAY_SQLSERVER_SN_TABLE,
        "sn_primary_key": settings.RELAY_SQLSERVER_SN_PRIMARY_KEY,
        "sn_updated_at_column": settings.RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN,
        "sn_column_map": dict(settings.RELAY_SQLSERVER_SN_COLUMN_MAP),
        "batch_size": int(settings.RELAY_SQLSERVER_BATCH_SIZE),
        "snapshot_max_age_hours": int(settings.RELAY_SN_SNAPSHOT_MAX_AGE_HOURS),
        "imap_fetch_enabled": bool(settings.IMAP_FETCH_ENABLED),
        "imap_poll_interval_minutes": int(settings.IMAP_POLL_INTERVAL_MINUTES),
        "imap_folder": settings.IMAP_FOLDER,
        "imap_fetch_limit": int(settings.IMAP_FETCH_LIMIT),
        "imap_unseen_only": bool(settings.IMAP_UNSEEN_ONLY),
        "imap_max_retries": int(settings.IMAP_MAX_RETRIES),
        "imap_archive_to_oss": bool(settings.IMAP_ARCHIVE_TO_OSS),
        **{key: value for key, value in values.items() if key in CONFIG_KEYS},
    }
    if "auto_send_enabled" not in values and legacy_auto_send:
        merged["auto_send_enabled"] = True
    if "rma_auto_send_enabled" not in values and legacy_rma_disabled:
        merged["rma_auto_send_enabled"] = False
    merged["auto_send_enabled"] = bool(merged["auto_send_enabled"])
    merged["auto_followup_enabled"] = bool(merged["auto_followup_enabled"])
    merged["rma_auto_send_enabled"] = bool(merged["rma_auto_send_enabled"])
    merged["auto_apply_min_confidence"] = max(0.0, min(1.0, float(merged["auto_apply_min_confidence"])))
    merged["auto_send_min_confidence"] = max(0.0, min(1.0, float(merged["auto_send_min_confidence"])))
    merged["confidence_threshold"] = max(0.0, min(1.0, float(merged["confidence_threshold"])))
    merged["max_follow_up"] = max(1, min(10, int(merged["max_follow_up"])))
    merged["relay_sqlserver_enabled"] = bool(merged["relay_sqlserver_enabled"])
    merged["relay_sn_sync_enabled"] = bool(merged["relay_sn_sync_enabled"])
    merged["batch_size"] = max(1, min(10000, int(merged["batch_size"])))
    merged["snapshot_max_age_hours"] = max(1, min(720, int(merged["snapshot_max_age_hours"])))
    merged["imap_fetch_enabled"] = bool(merged["imap_fetch_enabled"])
    merged["imap_poll_interval_minutes"] = max(1, min(1440, int(merged["imap_poll_interval_minutes"])))
    merged["imap_folder"] = str(merged["imap_folder"] or "INBOX")[:255]
    merged["imap_fetch_limit"] = max(1, min(1000, int(merged["imap_fetch_limit"])))
    merged["imap_unseen_only"] = bool(merged["imap_unseen_only"])
    merged["imap_max_retries"] = max(0, min(20, int(merged["imap_max_retries"])))
    merged["imap_archive_to_oss"] = bool(merged["imap_archive_to_oss"])
    return merged


def apply_runtime_config(values: dict[str, Any]) -> dict[str, Any]:
    global _runtime_cache
    config = _coerce_config(values)
    settings.AUTO_SEND_ENABLED = bool(config["auto_send_enabled"])
    settings.AUTO_FOLLOWUP_ENABLED = bool(config["auto_followup_enabled"])
    settings.RMA_AUTO_SEND_ENABLED = bool(config["rma_auto_send_enabled"])
    # Deprecated compatibility values are derived from the canonical switches.
    settings.REPLY_SEND_MODE = "auto_send" if settings.AUTO_SEND_ENABLED else "human_review"
    settings.RMA_AUTHORIZATION_ENABLED = settings.RMA_AUTO_SEND_ENABLED
    settings.AUTO_APPLY_MIN_CONFIDENCE = float(config["auto_apply_min_confidence"])
    settings.AUTO_SEND_MIN_CONFIDENCE = float(config["auto_send_min_confidence"])
    settings.CONFIDENCE_THRESHOLD = float(config["confidence_threshold"])
    settings.MAX_FOLLOW_UP = int(config["max_follow_up"])
    settings.RELAY_SQLSERVER_ENABLED = bool(config["relay_sqlserver_enabled"])
    settings.RELAY_SN_SYNC_ENABLED = bool(config["relay_sn_sync_enabled"])
    settings.RELAY_SQLSERVER_SN_SCHEMA = str(config["sn_schema"])
    settings.RELAY_SQLSERVER_SN_TABLE = str(config["sn_table"])
    settings.RELAY_SQLSERVER_SN_PRIMARY_KEY = str(config["sn_primary_key"])
    settings.RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN = str(config["sn_updated_at_column"])
    settings.RELAY_SQLSERVER_SN_COLUMN_MAP = dict(config["sn_column_map"])
    settings.RELAY_SQLSERVER_BATCH_SIZE = int(config["batch_size"])
    settings.RELAY_SN_SNAPSHOT_MAX_AGE_HOURS = int(config["snapshot_max_age_hours"])
    settings.IMAP_FETCH_ENABLED = bool(config["imap_fetch_enabled"])
    settings.IMAP_POLL_INTERVAL_MINUTES = int(config["imap_poll_interval_minutes"])
    settings.IMAP_FOLDER = str(config["imap_folder"])
    settings.IMAP_FETCH_LIMIT = int(config["imap_fetch_limit"])
    settings.IMAP_UNSEEN_ONLY = bool(config["imap_unseen_only"])
    settings.IMAP_MAX_RETRIES = int(config["imap_max_retries"])
    settings.IMAP_ARCHIVE_TO_OSS = bool(config["imap_archive_to_oss"])
    _runtime_cache = dict(config)
    return config


def read_runtime_config() -> dict[str, Any]:
    if _runtime_cache is not None:
        return dict(_runtime_cache)
    return apply_runtime_config(_read_legacy_file())


def _read_legacy_file() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data


def write_runtime_config(values: dict[str, Any]) -> dict[str, Any]:
    """Deprecated test compatibility helper; production updates use persist_runtime_config()."""
    return apply_runtime_config({**read_runtime_config(), **{key: value for key, value in values.items() if key in CONFIG_KEYS}})


def _group_for_key(key: str) -> str:
    return next(group for group, keys in CONFIG_GROUPS.items() if key in keys)


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    return "string"


async def load_runtime_config(session: AsyncSession, *, bootstrap: bool = True) -> dict[str, Any]:
    rows = (await session.execute(select(SystemConfig))).scalars().all()
    stored = {row.config_key: row.config_value for row in rows if row.config_key in CONFIG_KEYS}
    if not rows and bootstrap:
        config = _coerce_config(_read_legacy_file())
        for key, value in config.items():
            session.add(SystemConfig(
                config_key=key,
                config_group=_group_for_key(key),
                value_type=_value_type(value),
                config_value=value,
            ))
        await session.flush()
        stored = config
    return apply_runtime_config(stored)


async def persist_runtime_config(
    session: AsyncSession,
    values: dict[str, Any],
    *,
    user_id: int | None,
) -> dict[str, Any]:
    updates = {key: value for key, value in values.items() if key in CONFIG_KEYS}
    current = await load_runtime_config(session)
    next_config = _coerce_config({**current, **updates})
    existing = {
        row.config_key: row
        for row in (await session.execute(select(SystemConfig).where(SystemConfig.config_key.in_(updates)))).scalars().all()
    }
    for key in updates:
        value = next_config[key]
        row = existing.get(key)
        if row is None:
            row = SystemConfig(config_key=key, config_group=_group_for_key(key), value_type=_value_type(value), config_value=value)
            session.add(row)
        else:
            row.config_value = value
            row.value_type = _value_type(value)
            row.version = int(row.version or 0) + 1
        row.updated_by_user_id = user_id
    await session.flush()
    return next_config
