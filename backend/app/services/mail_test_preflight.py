from __future__ import annotations

import asyncio
import socket
import smtplib
import ssl
from typing import Any

from sqlalchemy import text

from app.config import settings
from app.core.database import AsyncSessionLocal
from app.services import imap_fetcher
from app.services.ai import multimodal_ai_configured, text_ai_configured
from app.services.mail_safety import test_mail_configuration_reasons
from app.services.rma_test_preflight import build_rma_test_preflight


class MailTestPreflightError(RuntimeError):
    def __init__(self, result: dict[str, Any]):
        super().__init__("MAIL_TEST_PREFLIGHT_FAILED")
        self.result = result


REQUIRED_DATABASE_REVISION = "y2t7u8v9w0x1"


class SmtpPreflightStageError(RuntimeError):
    def __init__(self, result: dict[str, Any]):
        super().__init__(str(result["error_code"]))
        self.result = result


def _masked_host(value: str) -> str:
    labels = [part for part in value.split(".") if part]
    if len(labels) >= 2:
        return f"***.{'.'.join(labels[-2:])}"
    return "***"


def _masked_account(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    visible = local[:2] if local else ""
    return f"{visible}***@{domain}"


def _smtp_error_code(exc: Exception, *, stage: str) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP_AUTH_FAILED"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "SMTP_SERVER_DISCONNECTED"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "SMTP_TIMEOUT"
    if isinstance(exc, ssl.SSLError):
        return "SMTP_TLS_FAILED"
    if isinstance(exc, OSError):
        return "SMTP_CONNECTION_FAILED"
    if stage == "noop":
        return "SMTP_NOOP_FAILED"
    return "SMTP_PROTOCOL_FAILED"


def _smtp_failure(*, stage: str, error_code: str) -> SmtpPreflightStageError:
    return SmtpPreflightStageError(
        {
            "status": "failed",
            "host": _masked_host(settings.SMTP_HOST),
            "account": _masked_account(settings.SMTP_USER),
            "tls": settings.SMTP_PORT in {465, 587},
            "stage": stage,
            "error_code": error_code,
            "authenticated": False,
            "noop": False,
            "messages_sent": 0,
        }
    )


async def _database_schema_preflight() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        current_revision = await session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
    return {
        "status": "ready" if current_revision == REQUIRED_DATABASE_REVISION else "failed",
        "current_revision": current_revision,
        "required_revision": REQUIRED_DATABASE_REVISION,
    }


def _smtp_login_preflight() -> dict[str, Any]:
    client: smtplib.SMTP | None = None
    stage = "tls" if settings.SMTP_PORT == 465 else "connect"
    try:
        if settings.SMTP_PORT == 465:
            client = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20)
        else:
            client = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20)
            if settings.SMTP_PORT == 587:
                stage = "tls"
                client.starttls()
        stage = "auth"
        client.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        stage = "noop"
        code, _ = client.noop()
        if int(code) >= 400:
            raise _smtp_failure(stage="noop", error_code="SMTP_NOOP_REJECTED")
        return {
            "status": "ready",
            "host": _masked_host(settings.SMTP_HOST),
            "account": _masked_account(settings.SMTP_USER),
            "tls": settings.SMTP_PORT in {465, 587},
            "authenticated": True,
            "noop": True,
            "stage": "complete",
            "messages_sent": 0,
        }
    except SmtpPreflightStageError:
        raise
    except Exception as exc:
        raise _smtp_failure(stage=stage, error_code=_smtp_error_code(exc, stage=stage)) from exc
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass


async def run_mail_test_preflight() -> dict[str, Any]:
    reasons = test_mail_configuration_reasons()
    offline_rma = build_rma_test_preflight().result
    reasons.extend(str(reason) for reason in offline_rma.get("reasons", []))
    integrations = {
        "oss": {
            "status": "ready"
            if bool(settings.OSS_ENDPOINT and settings.OSS_BUCKET and settings.OSS_ACCESS_KEY and settings.OSS_SECRET_KEY)
            else "failed"
        },
        "text_ai": {"status": "ready" if text_ai_configured() else "failed"},
        "multimodal_ai": {"status": "ready" if multimodal_ai_configured() else "failed"},
    }
    for name, item in integrations.items():
        if item["status"] != "ready":
            reasons.append(f"{name.upper()}_NOT_CONFIGURED")
    result: dict[str, Any] = {
        "status": "failed",
        "reasons": list(dict.fromkeys(reasons)),
        "database": None,
        "imap": None,
        "smtp": None,
        "offline_rma": offline_rma,
        "integrations": integrations,
        "messages_sent": 0,
    }
    try:
        result["database"] = await _database_schema_preflight()
    except Exception as exc:
        result["reasons"].append(f"DATABASE_PREFLIGHT_FAILED:{exc.__class__.__name__}")
        raise MailTestPreflightError(result) from exc
    if result["database"]["status"] != "ready":
        result["reasons"].append("DATABASE_SCHEMA_NOT_CURRENT")
    if reasons:
        raise MailTestPreflightError(result)
    if result["reasons"]:
        raise MailTestPreflightError(result)
    try:
        imap_result = await imap_fetcher.preflight_imap(folder_name=settings.IMAP_FOLDER)
        result["imap"] = {
            "status": imap_result.get("status"),
            "host": _masked_host(settings.IMAP_HOST),
            "account": _masked_account(settings.IMAP_USER),
            "folder": imap_result.get("folder"),
            "tls": bool(imap_result.get("tls")),
            "authenticated": bool(imap_result.get("authenticated")),
            "read_only": bool(imap_result.get("read_only")),
            "uid_validity": imap_result.get("uid_validity"),
            "messages_downloaded": 0,
            "flags_changed": False,
        }
    except Exception as exc:
        result["reasons"] = [f"IMAP_PREFLIGHT_FAILED:{exc.__class__.__name__}"]
        raise MailTestPreflightError(result) from exc
    try:
        result["smtp"] = await asyncio.to_thread(_smtp_login_preflight)
    except SmtpPreflightStageError as exc:
        result["smtp"] = exc.result
        result["reasons"] = [
            f"SMTP_PREFLIGHT_FAILED:{exc.result['stage']}:{exc.result['error_code']}"
        ]
        raise MailTestPreflightError(result) from exc
    result["status"] = "passed"
    return result
