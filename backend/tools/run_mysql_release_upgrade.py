from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncmy
import paramiko
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

# sshtunnel 0.4.0 still references the DSA key class removed by Paramiko 4.
# The release path uses password authentication; this alias only keeps
# sshtunnel's internal key-class lookup compatible with the installed version.
if not hasattr(paramiko, "DSSKey"):
    paramiko.DSSKey = paramiko.RSAKey  # type: ignore[attr-defined]

from sshtunnel import SSHTunnelForwarder

from app.config import settings


EXPECTED_DATABASE = "repair_system_test"
EXPECTED_HOSTS = {"127.0.0.1", "localhost", "::1"}
EXPECTED_LOCAL_PORT = 13307
EXPECTED_HEAD = "j7e1f2a3b4c5"
CRITICAL_TABLES = (
    "emails",
    "email_attachments",
    "repair_tickets",
    "repair_ticket_items",
    "reply_records",
    "export_sap",
    "ticket_rmas",
    "ticket_rma_items",
)
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BACKUP_DIR = PROJECT_ROOT.parent / "backups"


class ReleaseUpgradeError(RuntimeError):
    pass


def _config_value(name: str, default: str | None = None) -> str | None:
    explicit = os.environ.get(name)
    if explicit is not None:
        return explicit
    value = dotenv_values(PROJECT_ROOT / ".env").get(name)
    return str(value) if value not in {None, ""} else default


def _validated_target() -> Any:
    url = make_url(settings.DATABASE_URL)
    if (
        url.get_backend_name() != "mysql"
        or url.drivername not in {"mysql+asyncmy", "mysql+aiomysql"}
        or (url.host or "") not in EXPECTED_HOSTS
        or int(url.port or 3306) != EXPECTED_LOCAL_PORT
        or url.database != EXPECTED_DATABASE
        or not url.username
        or url.password is None
    ):
        raise ReleaseUpgradeError(
            "RELEASE_DATABASE_TARGET_MUST_BE_"
            "mysql+asyncmy://<user>:<password>@127.0.0.1:13307/repair_system_test"
        )
    return url.set(drivername="mysql+asyncmy")


def _validated_ssh_settings() -> dict[str, Any]:
    password = _config_value("SSH_PASSWORD")
    container = _config_value("SSH_MYSQL_CONTAINER", "repair-mysql") or "repair-mysql"
    if not password:
        raise ReleaseUpgradeError("SSH_PASSWORD_REQUIRED")
    if not SAFE_NAME.fullmatch(container):
        raise ReleaseUpgradeError("SSH_MYSQL_CONTAINER_INVALID")
    return {
        "host": _config_value("SSH_HOST", "47.100.20.214"),
        "port": int(_config_value("SSH_PORT", "22") or "22"),
        "username": _config_value("SSH_USER", "root"),
        "password": password,
        "container": container,
        "remote_host": _config_value(
            "SSH_REMOTE_HOST",
            _config_value("SSH_REMOTE_MYSQL_HOST", "127.0.0.1"),
        ),
        "remote_port": int(
            _config_value(
                "SSH_REMOTE_PORT",
                _config_value("SSH_REMOTE_MYSQL_PORT", "3307"),
            )
            or "3307"
        ),
    }


def _local_port_is_free() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", EXPECTED_LOCAL_PORT))
        except OSError:
            return False
    return True


def _backup_command(container: str, database: str) -> str:
    inner = (
        'exec mysqldump --single-transaction --quick --routines --triggers --events '
        '-uroot -p"$MYSQL_ROOT_PASSWORD" -- '
        f"{shlex.quote(database)}"
    )
    return (
        f"docker exec {shlex.quote(container)} "
        f"sh -lc {shlex.quote(inner)}"
    )


def _create_remote_backup(ssh: dict[str, Any], backup_dir: Path) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = backup_dir / f"{EXPECTED_DATABASE}_{timestamp}_before_{EXPECTED_HEAD}.sql"
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=ssh["host"],
            port=ssh["port"],
            username=ssh["username"],
            password=ssh["password"],
            timeout=10,
            auth_timeout=10,
            banner_timeout=10,
        )
        _, stdout, stderr = client.exec_command(
            _backup_command(ssh["container"], EXPECTED_DATABASE),
            timeout=30,
        )
        dump = stdout.read()
        error = stderr.read().decode("utf-8", errors="replace").strip()
        exit_status = stdout.channel.recv_exit_status()
    finally:
        client.close()
    if exit_status != 0:
        raise ReleaseUpgradeError(
            f"MYSQL_BACKUP_FAILED:{error[:500] or 'remote command failed'}"
        )
    if len(dump) < 1024 or b"dump" not in dump[:1024].lower():
        raise ReleaseUpgradeError("MYSQL_BACKUP_CONTENT_INVALID")
    path.write_bytes(dump)
    digest = hashlib.sha256(dump).hexdigest()
    return {
        "path": str(path.resolve()),
        "size_bytes": len(dump),
        "sha256": digest,
    }


async def _database_snapshot(url: Any) -> dict[str, Any]:
    connection = await asyncmy.connect(
        host=url.host,
        port=int(url.port),
        user=url.username,
        password=url.password,
        database=url.database,
        autocommit=True,
        connect_timeout=5,
    )
    try:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT DATABASE(), VERSION()")
            database_name, server_version = await cursor.fetchone()
            await cursor.execute(
                "SELECT version_num FROM alembic_version LIMIT 1"
            )
            revision_row = await cursor.fetchone()
            counts: dict[str, int] = {}
            for table in CRITICAL_TABLES:
                await cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                counts[table] = int((await cursor.fetchone())[0])
            await cursor.execute(
                "SELECT COUNT(*) FROM job_run_logs "
                "WHERE status IN ('running', 'retrying')"
            )
            active_jobs = int((await cursor.fetchone())[0])
    finally:
        connection.close()
    return {
        "database": str(database_name),
        "server_version": str(server_version),
        "revision": str(revision_row[0]) if revision_row else None,
        "critical_table_counts": counts,
        "active_jobs": active_jobs,
    }


def _run_step(
    name: str,
    command: list[str],
    *,
    timeout_seconds: int,
    env: dict[str, str],
) -> dict[str, Any]:
    print(f"RELEASE_STEP_START:{name}", flush=True)
    process = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        output, _ = process.communicate()
        raise ReleaseUpgradeError(
            f"RELEASE_STEP_TIMEOUT:{name}:{timeout_seconds}s:{output[-1000:]}"
        ) from exc
    if process.returncode != 0:
        raise ReleaseUpgradeError(
            f"RELEASE_STEP_FAILED:{name}:{process.returncode}:{output[-2000:]}"
        )
    print(f"RELEASE_STEP_OK:{name}", flush=True)
    return {
        "name": name,
        "command": command[1:],
        "status": "succeeded",
        "output_tail": output[-2000:],
    }


async def _execute(args: argparse.Namespace) -> dict[str, Any]:
    target = _validated_target()
    ssh = _validated_ssh_settings()
    if not _local_port_is_free():
        raise ReleaseUpgradeError("LOCAL_PORT_13307_ALREADY_IN_USE")

    backup = _create_remote_backup(ssh, args.backup_dir)
    print(
        f"RELEASE_BACKUP_OK:{backup['path']}:{backup['sha256']}",
        flush=True,
    )

    server = SSHTunnelForwarder(
        (ssh["host"], ssh["port"]),
        ssh_username=ssh["username"],
        ssh_password=ssh["password"],
        remote_bind_address=(ssh["remote_host"], ssh["remote_port"]),
        local_bind_address=("127.0.0.1", EXPECTED_LOCAL_PORT),
    )
    steps: list[dict[str, Any]] = []
    try:
        server.start()
        print("RELEASE_TUNNEL_READY:127.0.0.1:13307", flush=True)
        before = await _database_snapshot(target)
        if before["database"] != EXPECTED_DATABASE:
            raise ReleaseUpgradeError("RELEASE_DATABASE_NAME_MISMATCH")
        if before["active_jobs"] != 0:
            raise ReleaseUpgradeError(
                f"ACTIVE_JOBS_MUST_BE_ZERO:{before['active_jobs']}"
            )

        environment = dict(os.environ)
        environment["DATABASE_URL"] = target.render_as_string(
            hide_password=False
        )
        environment["PYTHONIOENCODING"] = "utf-8"
        python = sys.executable
        steps.append(
            _run_step(
                "alembic_current_before",
                [python, "-m", "alembic", "current"],
                timeout_seconds=args.step_timeout,
                env=environment,
            )
        )
        steps.append(
            _run_step(
                "alembic_upgrade_head",
                [python, "-m", "alembic", "upgrade", "head"],
                timeout_seconds=args.step_timeout,
                env=environment,
            )
        )
        steps.append(
            _run_step(
                "seed",
                [python, "-m", "app.seed"],
                timeout_seconds=args.step_timeout,
                env=environment,
            )
        )
        steps.append(
            _run_step(
                "schema_check",
                [python, "-m", "tools.check_sap_schema"],
                timeout_seconds=args.step_timeout,
                env=environment,
            )
        )
        steps.append(
            _run_step(
                "release_audit",
                [
                    python,
                    "-m",
                    "tools.audit_mail_release",
                    "--expected-database",
                    EXPECTED_DATABASE,
                    "--backup",
                    backup["path"],
                ],
                timeout_seconds=args.step_timeout,
                env=environment,
            )
        )
        after = await _database_snapshot(target)
        if after["revision"] != EXPECTED_HEAD:
            raise ReleaseUpgradeError(
                f"RELEASE_REVISION_MISMATCH:{after['revision']}:{EXPECTED_HEAD}"
            )
        if after["critical_table_counts"] != before["critical_table_counts"]:
            raise ReleaseUpgradeError("CRITICAL_TABLE_COUNTS_CHANGED")
        return {
            "status": "succeeded",
            "backup": backup,
            "before": before,
            "after": after,
            "steps": steps,
        }
    finally:
        if server.is_active:
            server.stop()
            print("RELEASE_TUNNEL_STOPPED", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Back up and upgrade only repair_system_test through the controlled "
            "127.0.0.1:13307 SSH tunnel."
        )
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
    )
    parser.add_argument(
        "--step-timeout",
        type=int,
        default=30,
        choices=range(10, 121),
        metavar="10..120",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "test-results" / "mysql_release_upgrade.json",
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(_execute(args))
    except Exception as exc:
        report = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:3000],
        }
        exit_code = 1
    else:
        exit_code = 0
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
