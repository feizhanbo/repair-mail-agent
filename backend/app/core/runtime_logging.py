from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.core.request_context import (
    get_client_ip,
    get_correlation_id,
    get_job_run_id,
    get_request_id,
    get_user_id,
)


_STANDARD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {
    "message", "asctime", "event", "service", "environment"
}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(password|passwd|pwd|authorization|cookie|access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|api[_-]?secret|secret[_-]?key|access[_-]?key[_-]?secret)"
    r"(\s*[=:]\s*)([^\s,;&]+)"
)
_SENSITIVE_HEADER = re.compile(r"(?i)\b(authorization|cookie)\s*:\s*[^\r\n,;]+")
_CONNECTION_PASSWORD = re.compile(r"(?i)(://[^:/\s]+:)([^@/\s]+)(@)")
_SIGNED_QUERY = re.compile(
    r"(?i)([?&](?:signature|x-oss-signature|ossaccesskeyid|security-token|token)=[^&\s]+)"
)
_EMAIL = re.compile(r"(?i)\b([a-z0-9._%+-])[^@\s]*(@[a-z0-9.-]+\.[a-z]{2,})\b")
_PHONE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")
_SENSITIVE_KEYS = (
    "password", "passwd", "pwd", "authorization", "cookie", "token", "api_key",
    "api_secret", "secret_key", "access_key", "database_url", "connection_string",
    "request_body", "response_body", "prompt", "raw_email", "email_body", "attachment_content",
)
_SAFE_TOKEN_METRIC_KEYS = {"input_tokens", "output_tokens", "total_tokens", "token_count"}


def mask_text(value: str) -> str:
    text = _SENSITIVE_HEADER.sub(lambda match: f"{match.group(1)}: ***", value)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}***", text)
    text = _CONNECTION_PASSWORD.sub(r"\1***\3", text)
    text = _SIGNED_QUERY.sub("[REDACTED_QUERY]", text)
    text = _EMAIL.sub(r"\1***\2", text)
    return _PHONE.sub(r"\1****\2", text)


def mask_value(value: Any, *, key: str = "") -> Any:
    lowered = key.casefold()
    if lowered in _SAFE_TOKEN_METRIC_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if any(part in lowered for part in _SENSITIVE_KEYS):
        return "***"
    if isinstance(value, str):
        return mask_text(value)[: max(256, settings.LOG_MAX_MESSAGE_LENGTH)]
    if isinstance(value, dict):
        return {str(k)[:100]: mask_value(v, key=str(k)) for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set)):
        return [mask_value(item, key=key) for item in list(value)[:100]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return mask_text(str(value))[:500]


class RuntimeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "service": settings.APP_NAME,
            "environment": settings.APP_ENV,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "logger": record.name,
            "module": record.module,
            "event": getattr(record, "event", "runtime_message"),
            "message": mask_text(message)[: settings.LOG_MAX_MESSAGE_LENGTH],
        }
        context = {
            "request_id": get_request_id(),
            "correlation_id": get_correlation_id(),
            "user_id": get_user_id(),
            "job_run_id": get_job_run_id(),
            "client_ip": get_client_ip(),
        }
        payload.update({key: value for key, value in context.items() if value is not None})
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and not key.startswith("_") and key not in payload:
                payload[key] = mask_value(value, key=key)
        if record.exc_info and settings.LOG_INCLUDE_TRACEBACK:
            rendered = "".join(traceback.format_exception(*record.exc_info))
            payload["exception"] = mask_text(rendered)[: settings.LOG_MAX_MESSAGE_LENGTH]
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class RuntimeSeverityFilter(logging.Filter):
    _EXTERNAL_PREFIXES = ("imap_", "smtp_", "oss_", "ai_", "sap_", "rma_pdf_")

    def filter(self, record: logging.LogRecord) -> bool:
        event = str(getattr(record, "event", ""))
        duration = getattr(record, "duration_ms", None)
        if (
            event.startswith(self._EXTERNAL_PREFIXES)
            and event.endswith("_completed")
            and isinstance(duration, (int, float))
            and duration >= settings.SLOW_EXTERNAL_THRESHOLD_MS
            and record.levelno < logging.WARNING
        ):
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
            record.slow = True
        return True


def _archive_name(default_name: str) -> str:
    path = Path(default_name)
    match = re.match(r"backend\.jsonl\.(\d{4}-\d{2}-\d{2})$", path.name)
    return str(path.with_name(f"backend-{match.group(1)}.jsonl")) if match else default_name


class DailyRuntimeFileHandler(logging.handlers.TimedRotatingFileHandler):
    def rotation_filename(self, default_name: str) -> str:
        return _archive_name(default_name)

    def getFilesToDelete(self) -> list[str]:
        candidates = sorted(Path(self.baseFilename).parent.glob("backend-????-??-??.jsonl"))
        overflow = len(candidates) - max(0, self.backupCount)
        return [str(path) for path in candidates[: max(0, overflow)]]


def configure_runtime_logging() -> None:
    formatter: logging.Formatter
    if settings.LOG_FORMAT.casefold() == "json":
        formatter = RuntimeJsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    handlers: list[logging.Handler] = []
    severity_filter = RuntimeSeverityFilter()
    if settings.LOG_STDOUT_ENABLED:
        stdout = logging.StreamHandler(sys.stdout)
        stdout.setFormatter(formatter)
        stdout.addFilter(severity_filter)
        handlers.append(stdout)
    if settings.LOG_FILE_ENABLED:
        log_dir = Path(settings.LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = DailyRuntimeFileHandler(
            log_dir / "backend.jsonl",
            when=settings.LOG_ROTATION_WHEN,
            interval=1,
            backupCount=max(1, settings.LOG_RETENTION_DAYS),
            encoding="utf-8",
            utc=True,
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(severity_filter)
        handlers.append(file_handler)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    for handler in handlers:
        root.addHandler(handler)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        child = logging.getLogger(name)
        child.handlers.clear()
        child.propagate = True


def runtime_log_directory_ready() -> bool:
    if not settings.LOG_FILE_ENABLED:
        return True
    path = Path(settings.LOG_DIR)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False
