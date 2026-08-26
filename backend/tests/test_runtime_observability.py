from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

import yaml
import pytest

from fastapi.testclient import TestClient

from app.core.request_context import (
    bind_request_context,
    normalize_correlation_id,
    normalize_request_id,
    reset_request_context,
)
from app.core.runtime_logging import DailyRuntimeFileHandler, RuntimeJsonFormatter, RuntimeSeverityFilter, mask_text, mask_value
from app.main import app
from app.core.database import get_session
from app.models import OperationLog


class _ScalarResult:
    def __init__(self, value=None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _AuthSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.committed = False

    async def execute(self, _statement):
        return _ScalarResult(None)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True

    async def get(self, _model, _identity):
        return None


class _ImmediateJsonCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[dict] = []
        self.setFormatter(RuntimeJsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.payloads.append(json.loads(self.format(record)))


def test_runtime_formatter_emits_valid_json_with_context_and_masked_exception() -> None:
    tokens = bind_request_context(
        request_id="req_test",
        correlation_id="corr_test",
        client_ip="127.0.0.1",
        user_agent="pytest",
        user_id=7,
        job_run_id=9,
    )
    try:
        try:
            raise RuntimeError("DATABASE_URL=mysql://user:secret@db/app password=abc")
        except RuntimeError:
            record = logging.getLogger("test.runtime").makeRecord(
                "test.runtime",
                logging.ERROR,
                __file__,
                1,
                "failed for %s",
                ("customer@example.com",),
                exc_info=__import__("sys").exc_info(),
                extra={"event": "test_failed", "authorization": "Bearer secret", "trace_id": "trace_test"},
            )
        payload = json.loads(RuntimeJsonFormatter().format(record))
    finally:
        reset_request_context(tokens)

    assert payload["event"] == "test_failed"
    assert payload["request_id"] == "req_test"
    assert payload["correlation_id"] == "corr_test"
    assert payload["user_id"] == 7
    assert payload["job_run_id"] == 9
    assert payload["trace_id"] == "trace_test"
    assert payload["authorization"] == "***"
    serialized = json.dumps(payload)
    assert "secret" not in serialized
    assert "password=abc" not in serialized
    assert "customer@example.com" not in serialized


def test_runtime_formatter_supports_standard_levels() -> None:
    formatter = RuntimeJsonFormatter()
    payloads = []
    for level in (logging.INFO, logging.WARNING, logging.ERROR):
        payloads.append(json.loads(formatter.format(logging.makeLogRecord({
            "name": "test.levels", "levelno": level, "levelname": logging.getLevelName(level),
            "msg": "level check", "event": "level_check",
        }))))
    assert [item["level"] for item in payloads] == ["INFO", "WARNING", "ERROR"]


def test_unhandled_500_has_traceback_and_same_request_id() -> None:
    async def _raise_runtime_error() -> None:
        raise RuntimeError("password=must-not-leak")

    before = list(app.router.routes)
    app.add_api_route("/__observability_test_500", _raise_runtime_error, methods=["GET"])
    capture = _ImmediateJsonCapture()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/__observability_test_500", headers={"X-Request-ID": "req_500_test"}
        )
    finally:
        root.removeHandler(capture)
        app.router.routes[:] = before

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "req_500_test"
    assert response.json()["request_id"] == "req_500_test"
    failure = next(item for item in capture.payloads if item["event"] == "unhandled_exception")
    assert failure["request_id"] == "req_500_test"
    assert failure["error_type"] == "RuntimeError"
    assert "Traceback (most recent call last)" in failure["exception"]
    assert "must-not-leak" not in json.dumps(failure)


def test_masker_handles_nested_secrets_signed_urls_and_phone() -> None:
    value = mask_value(
        {
            "password": "plain",
            "nested": {"api_key": "key", "access_token": "credential", "input_tokens": 123, "phone": "13812341234"},
            "url": "https://oss/path?Signature=secret&Expires=1",
        }
    )
    assert value["password"] == "***"
    assert value["nested"]["api_key"] == "***"
    assert value["nested"]["access_token"] == "***"
    assert value["nested"]["input_tokens"] == 123
    assert value["nested"]["phone"] == "138****1234"
    assert "secret" not in value["url"]


def test_request_id_generation_preserves_compatibility_prefix() -> None:
    generated = normalize_request_id(None)
    assert generated.startswith("req_")
    assert len(generated) == 36
    assert normalize_request_id("req_gateway") == "req_gateway"
    assert normalize_request_id("unsafe request id") != "unsafe request id"
    assert normalize_correlation_id("unsafe correlation id") != "unsafe correlation id"


def test_forwarded_ip_is_only_used_for_trusted_proxy(monkeypatch) -> None:
    from starlette.requests import Request
    from app.config import settings
    from app.main import _client_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXY_CIDRS", ["172.16.0.0/12"])
    headers = [(b"x-real-ip", b"198.51.100.20"), (b"x-forwarded-for", b"198.51.100.30")]
    untrusted = Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": ("203.0.113.5", 1234)})
    trusted = Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": ("172.20.0.3", 1234)})

    assert _client_ip(untrusted) == "203.0.113.5"
    assert _client_ip(trusted) == "198.51.100.20"


def test_http_response_uses_same_request_id_in_body_and_headers() -> None:
    client = TestClient(app)
    response = client.get(
        "/health",
        headers={"X-Request-ID": "req_gateway", "X-Correlation-ID": "corr_gateway"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_gateway"
    assert response.headers["X-Correlation-ID"] == "corr_gateway"


def test_mask_text_redacts_connection_password() -> None:
    masked = mask_text(
        "mysql+asyncmy://root:topsecret@mysql/db Authorization: Bearer token-value\nCookie: session=secret"
    )
    assert "topsecret" not in masked
    assert "root:***@mysql" in masked
    assert "token-value" not in masked
    assert "session=secret" not in masked


def test_daily_runtime_handler_uses_dated_archives_and_retention(tmp_path) -> None:
    handler = DailyRuntimeFileHandler(
        tmp_path / "backend.jsonl", when="midnight", backupCount=2, utc=True, encoding="utf-8"
    )
    try:
        rotated = handler.rotation_filename(str(tmp_path / "backend.jsonl.2026-08-26"))
        assert rotated.endswith("backend-2026-08-26.jsonl")
        for day in ("23", "24", "25"):
            (tmp_path / f"backend-2026-08-{day}.jsonl").write_text("{}\n", encoding="utf-8")
        assert handler.getFilesToDelete() == [str(tmp_path / "backend-2026-08-23.jsonl")]
    finally:
        handler.close()


def test_slow_external_call_is_promoted_to_warning(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "SLOW_EXTERNAL_THRESHOLD_MS", 5000)
    record = logging.makeLogRecord(
        {"levelno": logging.INFO, "levelname": "INFO", "event": "oss_upload_completed", "duration_ms": 5001}
    )
    assert RuntimeSeverityFilter().filter(record) is True
    assert record.levelno == logging.WARNING
    assert record.slow is True


def test_failed_login_and_anonymous_logout_are_audited() -> None:
    session = _AuthSession()

    async def fake_session() -> AsyncGenerator[_AuthSession, None]:
        yield session

    app.dependency_overrides[get_session] = fake_session
    try:
        client = TestClient(app)
        failed = client.post("/api/v1/auth/login", json={"username": "missing", "password": "bad"})
        logged_out = client.post("/api/v1/auth/logout")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert failed.status_code == 401
    assert logged_out.status_code == 200
    operations = [item for item in session.added if isinstance(item, OperationLog)]
    assert [item.operation_type for item in operations] == ["auth_login_failed", "auth_logout"]
    assert operations[0].after_data["reason_code"] == "INVALID_CREDENTIALS"
    assert "missing" not in json.dumps(operations[0].after_data)
    assert session.committed is True


@pytest.mark.anyio
async def test_enqueued_job_inherits_request_correlation_id() -> None:
    from app.services.jobs import enqueue_job

    class _JobSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def scalar(self, _statement):
            return None

        def add(self, value: object) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            for value in self.added:
                if getattr(value, "id", None) is None:
                    value.id = len(self.added)

    tokens = bind_request_context(
        request_id="req_parent", correlation_id="corr_parent", client_ip=None, user_agent=None
    )
    try:
        job = await enqueue_job(
            _JobSession(), job_type="export_generate", resource_type="emails", resource_id=None,
            idempotency_key="observability-correlation-test",
        )
    finally:
        reset_request_context(tokens)

    assert job.correlation_id == "corr_parent"


def test_production_compose_and_gateway_logging_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    for service in ("mysql", "backend-api", "frontend", "nginx"):
        assert compose["services"][service]["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "50m", "max-file": "5"},
        }
    backend = compose["services"]["backend-api"]
    assert backend["volumes"] == ["./logs:/app/logs"]
    assert backend["environment"]["LOG_FILE_ENABLED"] == "true"
    assert compose["services"]["nginx"]["ports"] == ["${NGINX_HTTP_PORT:-80}:80"]
    assert "127.0.0.1:13307:3306" in compose["services"]["mysql"]["ports"]

    nginx = (root / "nginx" / "default.conf").read_text(encoding="utf-8")
    assert "log_format main_json escape=json" in nginx
    assert "access_log /dev/stdout main_json" in nginx
    assert "error_log /dev/stderr warn" in nginx
    assert "proxy_set_header X-Request-ID req_$request_id" in nginx
    assert '"uri":"$uri"' in nginx
    assert "$request_uri" not in nginx
    assert "$http_referer" not in nginx


def test_disk_check_has_warning_and_critical_exit_contract() -> None:
    script = (Path(__file__).resolve().parents[2] / "tools" / "check_disk_usage.sh").read_text(encoding="utf-8")
    assert 'DISK_WARNING_PERCENT:-80' in script
    assert 'DISK_CRITICAL_PERCENT:-90' in script
    assert 'exit "$status"' in script
    assert "rm " not in script


def test_production_acceptance_script_covers_restart_and_persistence() -> None:
    script = (Path(__file__).resolve().parents[2] / "tools" / "verify_observability.sh").read_text(encoding="utf-8")
    assert "docker compose exec -T nginx nginx -t" in script
    assert "X-Request-ID" in script and "X-Correlation-ID" in script
    assert "logs/runtime/backend*.jsonl" in script
    assert "docker compose restart backend-api" in script
    assert "--force-recreate --no-deps backend-api" in script
    assert "mysql_mount_after" in script
    assert "check_disk_usage.sh" in script

    deploy = (Path(__file__).resolve().parents[2] / "deploy.sh").read_text(encoding="utf-8")
    assert "git status --porcelain" in deploy
    assert '"status":"ready"' in deploy
    assert "current_http_port" in deploy


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("login timeout expired", "SQLSERVER_CONNECTION_TIMEOUT"),
        ("08001 TCP Provider connection refused", "SQLSERVER_CONNECTION_FAILED"),
        ("name or service not known", "SQLSERVER_DNS_FAILED"),
        ("28000 login failed for user", "SQLSERVER_LOGIN_FAILED"),
        ("HYT00 query timeout", "SQLSERVER_QUERY_TIMEOUT"),
        ("40001 deadlock victim 1205", "SQLSERVER_DEADLOCK"),
        ("23000 duplicate key 2627", "SQLSERVER_DUPLICATE"),
        ("23000 check constraint", "SQLSERVER_CONSTRAINT_VIOLATION"),
        ("certificate verify TLS", "SQLSERVER_TLS_FAILED"),
        ("no data found", "SQLSERVER_DATA_NOT_FOUND"),
    ],
)
def test_sqlserver_error_classification(message: str, expected: str) -> None:
    from app.integrations.sap_middleware.sqlserver import _sqlserver_error_code

    assert _sqlserver_error_code(RuntimeError(message)) == expected


def test_sqlserver_transaction_rollback_classification() -> None:
    from app.integrations.sap_middleware.contracts import SapTransactionError
    from app.integrations.sap_middleware.sqlserver import _sqlserver_error_code

    assert _sqlserver_error_code(SapTransactionError("rolled back")) == "SQLSERVER_TRANSACTION_ROLLBACK"
