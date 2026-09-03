from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, tuple_
from sqlalchemy.engine import make_url
from dotenv import dotenv_values
import paramiko

if not hasattr(paramiko, "DSSKey"):
    paramiko.DSSKey = paramiko.RSAKey  # type: ignore[attr-defined]

from sshtunnel import SSHTunnelForwarder

from app.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.repair_items import normalize_board_code, normalize_board_name
from app.models import BoardCard, User
from app.schemas.business import BoardCardImportItem
from app.services.master_data import import_board_cards, parse_board_cards_file


EXPECTED_REVISION = "z3u8v9w0x1y2"
OVERSEAS_DEFAULT_CODE = "*"
OVERSEAS_DEFAULT_NAME = "OVERSEAS_DEFAULT_BEIJING"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ImportError(RuntimeError):
    pass


def _expected_database() -> str:
    return settings.database_name


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _row_snapshot(row: BoardCard) -> dict[str, Any]:
    return {
        column.name: _plain(getattr(row, column.name))
        for column in BoardCard.__table__.columns
    }


def _validate_target() -> None:
    url = make_url(settings.DATABASE_URL)
    allowed_databases = {name.strip() for name in settings.DESTRUCTIVE_TEST_DATABASE_ALLOWLIST if name.strip()}
    if (
        url.get_backend_name() != "mysql"
        or (url.host or "") not in {"127.0.0.1", "localhost", "::1"}
        or int(url.port or 3306) != 13307
        or not url.database
        or url.database not in allowed_databases
    ):
        raise ImportError("IMPORT_TARGET_MUST_MATCH_DATABASE_URL_ON_LOCAL_TUNNEL")


def _config_value(name: str, default: str | None = None) -> str | None:
    explicit = os.environ.get(name)
    if explicit is not None:
        return explicit
    value = dotenv_values(PROJECT_ROOT / ".env").get(name)
    return str(value) if value not in {None, ""} else default


def _open_tunnel() -> SSHTunnelForwarder:
    password = _config_value("SSH_PASSWORD")
    if not password:
        raise ImportError("SSH_PASSWORD_REQUIRED")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 13307))
        except OSError as exc:
            raise ImportError("LOCAL_TUNNEL_PORT_13307_NOT_FREE") from exc
    tunnel = SSHTunnelForwarder(
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
        local_bind_address=("127.0.0.1", 13307),
    )
    tunnel.start()
    return tunnel


def _add_overseas_default(
    items: list[BoardCardImportItem],
) -> list[BoardCardImportItem]:
    source = next(
        (
            item
            for item in items
            if item.return_location == "beijing"
            and item.shipping_address
            and item.shipping_contact
            and item.shipping_phone
        ),
        None,
    )
    if source is None:
        raise ImportError("COMPLETE_BEIJING_ROUTE_REQUIRED_FOR_OVERSEAS_DEFAULT")
    return [
        *items,
        BoardCardImportItem(
            board_code=OVERSEAS_DEFAULT_CODE,
            board_name=OVERSEAS_DEFAULT_NAME,
            return_location="beijing",
            route_type="scope_default",
            customer_scope="overseas",
            shipping_address=source.shipping_address,
            shipping_contact=source.shipping_contact,
            shipping_phone=source.shipping_phone,
            postal_code=source.postal_code,
            status="active",
            raw_data={
                "derived_from_source_row": source.source_row_no,
                "rule": "all overseas customers return to Beijing",
            },
        ),
    ]


def _business_keys(
    items: list[BoardCardImportItem],
) -> list[tuple[str, str | None, str, str]]:
    keys: list[tuple[str, str | None, str, str]] = []
    seen: set[tuple[str, str | None, str, str]] = set()
    for item in items:
        key = (
            normalize_board_code(item.board_code or item.material_code),
            normalize_board_name(item.board_name or item.material_name) or None,
            item.customer_scope,
            item.route_type,
        )
        if key not in seen:
            keys.append(key)
            seen.add(key)
    return keys


async def _load_rows(
    session: Any,
    keys: list[tuple[str, str | None, str, str]],
) -> list[BoardCard]:
    if not keys:
        return []
    return list(
        (
            await session.execute(
                select(BoardCard).where(
                    tuple_(
                        BoardCard.board_code,
                        BoardCard.board_name,
                        BoardCard.customer_scope,
                        BoardCard.route_type,
                    ).in_(keys)
                )
            )
        )
        .scalars()
        .all()
    )


async def _execute(args: argparse.Namespace) -> dict[str, Any]:
    _validate_target()
    source_path = Path(args.source).resolve()
    if not source_path.is_file():
        raise ImportError(f"SOURCE_FILE_NOT_FOUND:{source_path}")
    content = source_path.read_bytes()
    parsed, source_hash = parse_board_cards_file(
        content,
        filename=source_path.name,
    )
    items = _add_overseas_default(parsed)
    keys = _business_keys(items)

    async with AsyncSessionLocal() as session:
        revision = await session.scalar(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        )
        if revision != EXPECTED_REVISION:
            raise ImportError(
                f"DATABASE_REVISION_MISMATCH:{revision}:{EXPECTED_REVISION}"
            )
        operator = await session.scalar(
            select(User).where(User.status == "active").order_by(User.id)
        )
        if operator is None:
            raise ImportError("ACTIVE_IMPORT_OPERATOR_REQUIRED")
        before_rows = await _load_rows(session, keys)
        before = {str(row.id): _row_snapshot(row) for row in before_rows}
        preview = {
            "database": _expected_database(),
            "revision": revision,
            "source_file": str(source_path),
            "source_sha256": source_hash,
            "parsed_rows": len(parsed),
            "unique_business_keys": len(keys),
            "overseas_default_added": True,
            "matching_rows_before": len(before_rows),
            "apply": bool(args.apply),
        }
        if not args.apply:
            return {"status": "dry_run", **preview}

        result = await import_board_cards(
            session,
            items=items,
            source_file_name=source_path.name,
            source_file_hash=source_hash,
            user_id=operator.id,
        )
        await session.flush()
        after_rows = await _load_rows(session, keys)
        after = {str(row.id): _row_snapshot(row) for row in after_rows}
        created_ids = sorted(
            int(row_id) for row_id in set(after).difference(before)
        )
        changed_before = {
            row_id: snapshot
            for row_id, snapshot in before.items()
            if after.get(row_id) != snapshot
        }
        manifest = {
            "status": "applied",
            **preview,
            "operator_user_id": operator.id,
            "result": result,
            "rollback": {
                "created_ids_to_delete": created_ids,
                "updated_rows_to_restore": changed_before,
            },
            "after": {
                "matching_rows": len(after_rows),
                "complete_routes": sum(
                    bool(
                        row.shipping_address
                        and row.shipping_contact
                        and row.shipping_phone
                    )
                    for row in after_rows
                ),
                "overseas_defaults": sum(
                    row.customer_scope == "overseas"
                    and row.route_type == "scope_default"
                    and row.status == "active"
                    for row in after_rows
                ),
            },
        }
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(_plain(manifest), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        await session.commit()
        manifest["manifest_path"] = str(output_path)
        return manifest


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return await _execute(args)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or apply the board return-route data alignment to the "
            "guarded DATABASE_URL database."
        )
    )
    parser.add_argument("--source", required=True)
    parser.add_argument(
        "--output",
        default="../test-results/field-alignment-import-manifest.json",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag the command is read-only.",
    )
    args = parser.parse_args()
    tunnel: SSHTunnelForwarder | None = None
    try:
        tunnel = _open_tunnel()
        result = asyncio.run(_run(args))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if tunnel is not None:
            tunnel.stop()
    print(json.dumps(_plain(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
