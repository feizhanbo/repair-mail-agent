from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
TESTS = [
    "tests/test_sap_rma1_rma2_refactor.py",
    "tests/test_sap_rma_closed_loop.py",
    "tests/test_external_relay_placeholder.py",
    "tests/test_sap_sn_sync.py",
    "tests/test_test_relay_server.py",
]


def _run(name: str, command: list[str]) -> dict[str, object]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    result = subprocess.run(
        command,
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "name": name,
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "returncode": result.returncode,
        "stdout": result.stdout[-8000:],
        "stderr": result.stderr[-8000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the offline SAP RMA1/RMA2 contract suite")
    parser.add_argument("--report", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    checks = [
        _run("alembic_offline_sql", [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"]),
        _run("sap_rma1_rma2_tests", [sys.executable, "-m", "pytest", "-q", *TESTS]),
    ]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_sqlserver_simulation",
        "remote_sqlserver_mutated": False,
        "checks": checks,
        "status": "PASS" if all(row["status"] == "PASS" for row in checks) else "FAIL",
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
