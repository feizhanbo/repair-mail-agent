from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.config import settings

CONFIG_KEYS = {
    "auto_send_enabled",
    "reply_send_mode",
    "auto_send_min_confidence",
    "confidence_threshold",
    "max_follow_up",
}

DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "auto_send_enabled": False,
    "reply_send_mode": "human_review",
    "auto_send_min_confidence": 0.85,
    "confidence_threshold": 0.7,
    "max_follow_up": 3,
}


def _path() -> Path:
    return Path(settings.RUNTIME_CONFIG_PATH)


def _coerce_config(values: dict[str, Any]) -> dict[str, Any]:
    merged = {
        **DEFAULT_RUNTIME_CONFIG,
        "auto_send_enabled": bool(settings.AUTO_SEND_ENABLED),
        "reply_send_mode": settings.REPLY_SEND_MODE,
        "auto_send_min_confidence": float(settings.AUTO_SEND_MIN_CONFIDENCE),
        "confidence_threshold": float(settings.CONFIDENCE_THRESHOLD),
        "max_follow_up": int(settings.MAX_FOLLOW_UP),
        **{key: value for key, value in values.items() if key in CONFIG_KEYS},
    }
    mode = str(merged["reply_send_mode"] or "human_review")
    merged["reply_send_mode"] = mode if mode in {"human_review", "auto_send"} else "human_review"
    merged["auto_send_enabled"] = bool(merged["auto_send_enabled"])
    merged["auto_send_min_confidence"] = max(0.0, min(1.0, float(merged["auto_send_min_confidence"])))
    merged["confidence_threshold"] = max(0.0, min(1.0, float(merged["confidence_threshold"])))
    merged["max_follow_up"] = max(1, min(10, int(merged["max_follow_up"])))
    return merged


def apply_runtime_config(values: dict[str, Any]) -> dict[str, Any]:
    config = _coerce_config(values)
    settings.AUTO_SEND_ENABLED = bool(config["auto_send_enabled"])
    settings.REPLY_SEND_MODE = str(config["reply_send_mode"])
    settings.AUTO_SEND_MIN_CONFIDENCE = float(config["auto_send_min_confidence"])
    settings.CONFIDENCE_THRESHOLD = float(config["confidence_threshold"])
    settings.MAX_FOLLOW_UP = int(config["max_follow_up"])
    return config


def read_runtime_config() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return apply_runtime_config({})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return apply_runtime_config(data)


def write_runtime_config(values: dict[str, Any]) -> dict[str, Any]:
    current = read_runtime_config()
    next_config = apply_runtime_config({**current, **{key: value for key, value in values.items() if key in CONFIG_KEYS}})
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(next_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)
    return next_config

