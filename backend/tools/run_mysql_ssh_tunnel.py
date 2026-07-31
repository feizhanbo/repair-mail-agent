from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import paramiko
from dotenv import dotenv_values

# sshtunnel 0.4.0 still references the DSA key class removed by Paramiko 4.
# Password authentication is used here; the alias only keeps sshtunnel's
# internal key-class lookup compatible with the installed Paramiko release.
if not hasattr(paramiko, "DSSKey"):
    paramiko.DSSKey = paramiko.RSAKey  # type: ignore[attr-defined]

from sshtunnel import SSHTunnelForwarder


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _config_value(name: str, default: str | None = None) -> str | None:
    explicit = os.environ.get(name)
    if explicit is not None:
        return explicit
    value = dotenv_values(PROJECT_ROOT / ".env").get(name)
    return str(value) if value not in {None, ""} else default


def main() -> None:
    local_host = _config_value("SSH_LOCAL_HOST", "127.0.0.1") or "127.0.0.1"
    if local_host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("SSH_TUNNEL_LOOPBACK_BIND_REQUIRED")
    password = _config_value("SSH_PASSWORD")
    if not password:
        raise SystemExit("SSH_PASSWORD_REQUIRED")
    server = SSHTunnelForwarder(
        (
            _config_value("SSH_HOST", "47.100.20.214"),
            int(_config_value("SSH_PORT", "22") or "22"),
        ),
        ssh_username=_config_value("SSH_USER", "root"),
        ssh_password=password,
        remote_bind_address=(
            _config_value(
                "SSH_REMOTE_HOST",
                _config_value("SSH_REMOTE_MYSQL_HOST", "127.0.0.1"),
            ),
            int(
                _config_value(
                    "SSH_REMOTE_PORT",
                    _config_value("SSH_REMOTE_MYSQL_PORT", "3307"),
                )
                or "3307"
            ),
        ),
        local_bind_address=(
            local_host,
            int(_config_value("SSH_LOCAL_PORT", "13307") or "13307"),
        ),
    )
    server.start()
    print(
        f"MYSQL_SSH_TUNNEL_READY:{server.local_bind_host}:{server.local_bind_port}",
        flush=True,
    )
    stopping = False

    def stop(*_args) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopping:
            time.sleep(1)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
