from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools import run_mysql_release_upgrade as release
from tools import audit_mail_release, check_sap_schema
from app.config import Settings


def test_release_target_is_strictly_limited_to_loopback_test_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release.settings,
        "DATABASE_URL",
        "mysql+asyncmy://user:password@127.0.0.1:13307/repair_system_test",
    )
    target = release._validated_target()
    assert target.database == "repair_system_test"
    assert target.port == 13307
    assert target.drivername == "mysql+asyncmy"

    monkeypatch.setattr(
        release.settings,
        "DATABASE_URL",
        "mysql+aiomysql://user:password@127.0.0.1:13307/repair_system_test",
    )
    assert release._validated_target().drivername == "mysql+asyncmy"

    for unsafe in (
        "mysql+asyncmy://user:password@127.0.0.1:13307/repair_system_dev",
        "mysql+asyncmy://user:password@47.100.20.214:3307/repair_system_test",
        "mysql+pymysql://user:password@127.0.0.1:13307/repair_system_test",
    ):
        monkeypatch.setattr(release.settings, "DATABASE_URL", unsafe)
        with pytest.raises(release.ReleaseUpgradeError):
            release._validated_target()


def test_remote_backup_command_uses_container_password_without_interpolation() -> None:
    command = release._backup_command("repair-mysql", "repair_system_test")
    assert command.startswith("docker exec repair-mysql sh -lc ")
    assert 'MYSQL_ROOT_PASSWORD' in command
    assert "repair_system_test" in command
    assert "change-me" not in command


def test_ssh_settings_require_password_and_safe_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSH_PASSWORD", raising=False)
    monkeypatch.setattr(release, "_config_value", lambda name, default=None: default)
    with pytest.raises(release.ReleaseUpgradeError, match="SSH_PASSWORD_REQUIRED"):
        release._validated_ssh_settings()

    values = {
        "SSH_PASSWORD": "not-printed",
        "SSH_MYSQL_CONTAINER": "repair-mysql;rm",
    }
    monkeypatch.setattr(
        release,
        "_config_value",
        lambda name, default=None: values.get(name, default),
    )
    with pytest.raises(
        release.ReleaseUpgradeError,
        match="SSH_MYSQL_CONTAINER_INVALID",
    ):
        release._validated_ssh_settings()


def test_upgrade_step_timeout_is_reported_and_process_is_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(returncode=None)
    process.communicate = lambda timeout=None: (_ for _ in ()).throw(
        release.subprocess.TimeoutExpired(["python"], timeout)
    )
    process.kill = lambda: setattr(process, "returncode", -9)

    def communicate_after_kill(timeout=None):
        if process.returncode == -9:
            return ("terminated", None)
        raise AssertionError("unexpected")

    original_communicate = process.communicate

    def communicate(timeout=None):
        if process.returncode == -9:
            return communicate_after_kill(timeout)
        return original_communicate(timeout)

    process.communicate = communicate
    monkeypatch.setattr(release.subprocess, "Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(release.ReleaseUpgradeError, match="RELEASE_STEP_TIMEOUT"):
        release._run_step(
            "migration",
            ["python", "-m", "alembic", "upgrade", "head"],
            timeout_seconds=10,
            env={},
        )


def test_schema_audit_constants_match_release_head_and_models() -> None:
    from app.models import Base

    assert check_sap_schema.EXPECTED_REVISION == "m0h5c6d7e8f9"
    assert check_sap_schema.EXPECTED_BUSINESS_TABLE_COUNT == len(Base.metadata.tables)
    assert audit_mail_release.REQUIRED_REVISION == check_sap_schema.EXPECTED_REVISION


def test_release_audit_rejects_stale_revision_and_invalid_close_route() -> None:
    report = {
        "database": {"matches_expected": True, "is_current": False},
        "schema": {
            "uid_validity_present": True,
            "receipt_columns_complete": True,
            "uid_validity_unique_constraint_present": True,
            "device_received_foreign_key": [{"constraint_name": "fk"}],
        },
        "only_rma_issued_and_archived_enabled": False,
        "backup": {"exists": True},
    }

    assert audit_mail_release._audit_failures(report, require_backup=True) == [
        "ALEMBIC_REVISION_NOT_CURRENT",
        "CLOSE_TRANSITION_GATE_INVALID",
    ]


def test_full_migration_chain_can_render_offline_sql() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "PRECONDITION: export_sap must contain zero legacy rows" in result.stdout
    assert "m0h5c6d7e8f9" in result.stdout


def test_settings_normalize_legacy_aiomysql_url_without_changing_target() -> None:
    configured = Settings(
        _env_file=None,
        DATABASE_URL="mysql+aiomysql://user:secret@127.0.0.1:13307/repair_system_test",
    )

    assert configured.DATABASE_URL == (
        "mysql+asyncmy://user:secret@127.0.0.1:13307/repair_system_test"
    )
