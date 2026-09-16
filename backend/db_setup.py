from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import BACKEND_DIR, settings

def _parse_url(url: str) -> tuple[str, str, str, str, int | None, str]:
    parsed = urlparse(url)
    return (
        parsed.scheme,
        parsed.username or "",
        parsed.password or "",
        parsed.hostname or "",
        parsed.port,
        parsed.path.lstrip("/"),
    )


def _build_url(scheme: str, user: str, password: str, host: str, port: int | None, db_name: str | None = None) -> str:
    auth = f"{user}:{password}" if password else user
    port_part = f":{port}" if port else ""
    db_part = f"/{db_name}" if db_name else ""
    return f"{scheme}://{auth}@{host}{port_part}{db_part}"


async def _create_database_if_not_exists(nodb_url: str, db_name: str) -> None:
    engine = create_async_engine(nodb_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
            await conn.commit()
    finally:
        await engine.dispose()


async def _run_subprocess(cmd: str, cwd: str, env: dict[str, str]) -> None:
    process = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if stdout:
        print(stdout.decode().rstrip())
    if stderr:
        print(stderr.decode().rstrip())
    if process.returncode != 0:
        raise RuntimeError(f"command failed with exit code {process.returncode}: {cmd}")


async def _main() -> None:
    scheme, user, password, host, port, target_db_name = _parse_url(settings.DATABASE_URL)
    if not target_db_name:
        raise SystemExit("DATABASE_URL must include a database name")
    nodb_url = _build_url(scheme, user, password, host, port)
    test_db_url = settings.DATABASE_URL

    print(f"Creating database {target_db_name}...")
    try:
        await _create_database_if_not_exists(nodb_url, target_db_name)
    except Exception as exc:
        print(f"Failed to create database: {exc}")
        raise SystemExit(1) from exc
    print("Database created (or already exists).")

    env = os.environ.copy()
    env["DATABASE_URL"] = test_db_url

    print("Running Alembic migrations...")
    try:
        await _run_subprocess("alembic upgrade head", str(BACKEND_DIR), env)
    except Exception as exc:
        print(f"Migration failed: {exc}")
        raise SystemExit(1) from exc

    print("Running seed data...")
    try:
        await _run_subprocess("python -m app.seed", str(BACKEND_DIR), env)
    except Exception as exc:
        print(f"Seed failed: {exc}")
        raise SystemExit(1) from exc

    print(f"Done. {target_db_name} is ready.")


if __name__ == "__main__":
    asyncio.run(_main())
