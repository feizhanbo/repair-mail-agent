from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.database import AsyncSessionLocal, engine
from tools.import_field_alignment_data import (
    EXPECTED_DATABASE,
    EXPECTED_REVISION,
    _open_tunnel,
    _plain,
    _validate_target,
)


async def _audit() -> dict[str, Any]:
    _validate_target()
    async with AsyncSessionLocal() as session:
        revision = await session.scalar(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        )
        database = await session.scalar(text("SELECT DATABASE()"))
        server_version = await session.scalar(text("SELECT VERSION()"))
        if revision != EXPECTED_REVISION or database != EXPECTED_DATABASE:
            raise RuntimeError(
                f"TARGET_MISMATCH:{database}:{revision}:{EXPECTED_REVISION}"
            )
        queries = {
            "sn_assets": """
                SELECT COUNT(*) total,
                       SUM(asset_status = 'valid') valid,
                       SUM(customer_code IS NULL OR customer_code = '') missing_customer_code,
                       SUM(material_code IS NULL OR material_code = '') missing_material_code
                FROM sn_assets
            """,
            "board_cards": """
                SELECT COUNT(*) total,
                       SUM(status = 'active') active,
                       COUNT(DISTINCT CASE WHEN route_type = 'board_rule'
                                          THEN board_code END) distinct_board_codes,
                       SUM(status = 'active' AND
                           (shipping_address IS NULL OR shipping_address = '' OR
                            shipping_contact IS NULL OR shipping_contact = '' OR
                            shipping_phone IS NULL OR shipping_phone = '')) incomplete_active,
                       SUM(status = 'active' AND customer_scope = 'overseas'
                           AND route_type = 'scope_default') overseas_defaults
                FROM board_cards
            """,
            "board_route_breakdown": """
                SELECT customer_scope, route_type, return_location, status, COUNT(*) count
                FROM board_cards
                GROUP BY customer_scope, route_type, return_location, status
                ORDER BY customer_scope, route_type, return_location, status
            """,
            "board_cross_location_conflicts": """
                SELECT board_code, COUNT(DISTINCT return_location) location_count
                FROM board_cards
                WHERE status = 'active' AND customer_scope = 'domestic'
                  AND route_type = 'board_rule'
                GROUP BY board_code
                HAVING COUNT(DISTINCT return_location) > 1
                ORDER BY board_code
            """,
            "policies": """
                SELECT COUNT(*) total,
                       SUM(enabled = 1) enabled,
                       SUM(customer_scope IS NULL OR customer_scope = '') scope_unknown,
                       SUM(charge_status = 'manual_confirmation') manual_confirmation
                FROM customer_service_policies
            """,
            "tickets": """
                SELECT COUNT(*) total,
                       SUM(customer_scope IS NULL OR customer_scope = '') scope_unknown,
                       SUM(charge_status = 'manual_confirmation') manual_confirmation,
                       SUM(policy_resolution_status = 'resolved') policy_resolved
                FROM repair_tickets
            """,
            "ticket_policy_status": """
                SELECT policy_resolution_status, charge_status, customer_scope, COUNT(*) count
                FROM repair_tickets
                GROUP BY policy_resolution_status, charge_status, customer_scope
                ORDER BY policy_resolution_status, charge_status, customer_scope
            """,
            "sap_exports": """
                SELECT COUNT(*) total,
                       SUM(mailing_address IS NULL OR mailing_address = '') missing_mailing_address,
                       SUM(contact_person IS NULL OR contact_person = '') missing_contact,
                       SUM(contact_phone IS NULL OR contact_phone = '') missing_phone,
                       SUM(charge_status = 'manual_confirmation') manual_confirmation
                FROM export_sap
            """,
            "sap_charge_status": """
                SELECT charge_status, COUNT(*) count
                FROM export_sap
                GROUP BY charge_status
                ORDER BY charge_status
            """,
        }
        report: dict[str, Any] = {
            "database": database,
            "server_version": server_version,
            "revision": revision,
        }
        for name, query in queries.items():
            result = await session.execute(text(query))
            report[name] = [dict(row) for row in result.mappings().all()]
        return report


async def _run() -> dict[str, Any]:
    try:
        return await _audit()
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only post-import field-alignment database audit."
    )
    parser.add_argument(
        "--output",
        default="../test-results/field-alignment-database-audit.json",
    )
    args = parser.parse_args()
    tunnel = None
    try:
        tunnel = _open_tunnel()
        report = asyncio.run(_run())
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(_plain(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report["report_path"] = str(output)
        print(json.dumps(_plain(report), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if tunnel is not None:
            tunnel.stop()


if __name__ == "__main__":
    raise SystemExit(main())
