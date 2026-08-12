from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, model_validator


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SCENARIOS = {"normal", "delayed", "partial", "invalid_rma", "multi_rma", "timeout", "late"}


class RelayRecord(BaseModel):
    source_request_id: str | None = Field(default=None, min_length=8, max_length=128)
    submission_key: str | None = Field(default=None, min_length=8, max_length=128)
    ticket_id: int | None = None
    ticket_item_id: int | None = None
    relay_export_id: int | None = None
    sn: str = Field(min_length=1, max_length=100)
    model_config = {"extra": "allow"}

    @model_validator(mode="after")
    def require_source_request_id(self) -> "RelayRecord":
        value = self.source_request_id or self.submission_key
        if not value:
            raise ValueError("SOURCE_REQUEST_ID_REQUIRED")
        self.source_request_id = value
        return self


class RelayBatch(BaseModel):
    items: list[RelayRecord] = Field(min_length=1)


class RelayQuery(BaseModel):
    source_request_ids: list[str] = Field(min_length=1)


class RelayControl(BaseModel):
    scenario: str = "normal"
    delay_seconds: int = Field(default=0, ge=0, le=31_536_000)
    rma_no: str | None = None

    @model_validator(mode="after")
    def validate_rma_no(self) -> "RelayControl":
        if self.rma_no is not None and not re.fullmatch(r"\d{10}|INVALID-RMA", self.rma_no):
            raise ValueError("TEST_RELAY_RMA_NO_INVALID")
        return self


class TestRelayStore:
    __test__ = False

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.call_id_namespace = hashlib.sha256(
            str(self.path).casefold().encode("utf-8")
        ).hexdigest()[:10].upper()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_key TEXT NOT NULL UNIQUE,
                    call_id TEXT NOT NULL UNIQUE,
                    ticket_id INTEGER,
                    ticket_item_id INTEGER,
                    sn TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    scenario TEXT NOT NULL DEFAULT 'normal',
                    rma_no TEXT,
                    available_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ticket_rmas (
                    ticket_key TEXT PRIMARY KEY,
                    rma_no TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(records)").fetchall()}
            if "source_request_id" not in columns:
                db.execute("ALTER TABLE records ADD COLUMN source_request_id TEXT")
                db.execute("UPDATE records SET source_request_id = submission_key WHERE source_request_id IS NULL")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uk_records_source_request_id "
                "ON records(source_request_id)"
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _setting(self, db: sqlite3.Connection, key: str, default: str) -> str:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def _next_rma(self, db: sqlite3.Connection, ticket_key: str) -> str:
        existing = db.execute(
            "SELECT rma_no FROM ticket_rmas WHERE ticket_key = ?", (ticket_key,)
        ).fetchone()
        if existing:
            return str(existing["rma_no"])
        prefix = self._now().astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d")
        row = db.execute(
            "SELECT COUNT(*) AS count FROM ticket_rmas WHERE rma_no LIKE ?", (f"{prefix}%",)
        ).fetchone()
        sequence = int(row["count"]) + 1
        if sequence > 99:
            raise RuntimeError("TEST_RELAY_DAILY_RMA_SEQUENCE_EXHAUSTED")
        rma_no = f"{prefix}{sequence:02d}"
        db.execute(
            "INSERT INTO ticket_rmas(ticket_key, rma_no) VALUES (?, ?)", (ticket_key, rma_no)
        )
        return rma_no

    def _create_in_db(self, db: sqlite3.Connection, payload: RelayRecord) -> dict[str, Any]:
        source_request_id = str(payload.source_request_id)
        existing = db.execute(
            "SELECT call_id FROM records WHERE source_request_id = ?", (source_request_id,)
        ).fetchone()
        if existing:
            return {
                "status": "succeeded",
                "source_request_id": source_request_id,
                "remote_record_key": str(existing["call_id"]),
                "idempotent_reuse": True,
            }
        record_id = int(db.execute("SELECT COALESCE(MAX(id), 0) + 1 AS id FROM records").fetchone()["id"])
        call_id = f"TESTCALL-{self.call_id_namespace}-{record_id:08d}"
        scenario = self._setting(db, "default_scenario", "normal")
        delay = int(self._setting(db, "default_delay_seconds", "0"))
        ticket_key = str(payload.ticket_id or payload.relay_export_id or source_request_id)
        if scenario == "multi_rma":
            ticket_key = f"{ticket_key}:{payload.ticket_item_id or payload.sn}"
        rma_no = self._next_rma(db, ticket_key)
        fixed_rma_no = self._setting(db, "default_rma_no", "").strip()
        if fixed_rma_no:
            rma_no = fixed_rma_no
        if scenario == "invalid_rma":
            rma_no = "INVALID-RMA"
        available_at = self._now() + timedelta(seconds=delay)
        if scenario in {"timeout", "late"}:
            available_at = self._now() + timedelta(days=365)
        db.execute(
            """
                INSERT INTO records(
                    id, submission_key, source_request_id, call_id, ticket_id, ticket_item_id, sn,
                    payload_json, scenario, rma_no, available_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                source_request_id,
                source_request_id,
                call_id,
                payload.ticket_id,
                payload.ticket_item_id,
                payload.sn,
                json.dumps(payload.model_dump(), ensure_ascii=False, default=str),
                scenario,
                rma_no,
                available_at.isoformat(),
                self._now().isoformat(),
            ),
        )
        return {
            "status": "succeeded",
            "source_request_id": source_request_id,
            "remote_record_key": call_id,
            "idempotent_reuse": False,
        }

    def create(self, payload: RelayRecord) -> dict[str, Any]:
        with self.connection() as db:
            return self._create_in_db(db, payload)

    def create_batch(self, payload: RelayBatch) -> dict[str, Any]:
        with self.connection() as db:
            rows = [self._create_in_db(db, item) for item in payload.items]
            return {"status": "succeeded", "items": rows}

    def query(self, source_request_ids: list[str]) -> list[dict[str, Any]]:
        if not source_request_ids:
            return []
        with self.connection() as db:
            placeholders = ",".join("?" for _ in source_request_ids)
            rows = db.execute(
                f"SELECT * FROM records WHERE source_request_id IN ({placeholders})",
                source_request_ids,
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                rma_no: str | None = None
                if not (row["scenario"] == "partial" and int(row["id"]) % 2 == 0):
                    available = datetime.fromisoformat(str(row["available_at"]))
                    if self._now() >= available:
                        rma_no = row["rma_no"]
                result.append(
                    {
                        "source_request_id": row["source_request_id"],
                        "sn": row["sn"],
                        "rma_no": rma_no,
                    }
                )
            return result

    def get(self, call_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM records WHERE call_id = ?", (call_id,)).fetchone()
            if row is None:
                return None
            if row["scenario"] == "partial" and int(row["id"]) % 2 == 0:
                return {"status": "waiting_rma", "remote_call_id": call_id, "rma_no": None}
            available = datetime.fromisoformat(str(row["available_at"]))
            if self._now() < available:
                return {"status": "waiting_rma", "remote_call_id": call_id, "rma_no": None}
            return {
                "status": "rma_received" if row["rma_no"] else "waiting_rma",
                "remote_call_id": call_id,
                "rma_no": row["rma_no"],
            }

    def configure(self, control: RelayControl) -> dict[str, Any]:
        if control.scenario not in SCENARIOS:
            raise ValueError("TEST_RELAY_SCENARIO_INVALID")
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES ('default_scenario', ?)",
                (control.scenario,),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES ('default_delay_seconds', ?)",
                (str(control.delay_seconds),),
            )
            if control.rma_no:
                db.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES ('default_rma_no', ?)",
                    (control.rma_no,),
                )
            else:
                db.execute("DELETE FROM settings WHERE key = 'default_rma_no'")
        return control.model_dump()

    def update(self, call_id: str, control: RelayControl) -> dict[str, Any] | None:
        if control.scenario not in SCENARIOS:
            raise ValueError("TEST_RELAY_SCENARIO_INVALID")
        available_at = self._now() + timedelta(seconds=control.delay_seconds)
        with self.connection() as db:
            result = db.execute(
                """
                UPDATE records
                SET scenario = ?, available_at = ?, rma_no = COALESCE(?, rma_no)
                WHERE call_id = ?
                """,
                (control.scenario, available_at.isoformat(), control.rma_no, call_id),
            )
            return control.model_dump() if result.rowcount else None

    def reset(self) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM records")
            db.execute("DELETE FROM ticket_rmas")
            db.execute("DELETE FROM settings")


def create_app(*, database: Path, token: str) -> FastAPI:
    if len(token) < 24:
        raise ValueError("TEST_RELAY_TOKEN_TOO_SHORT")
    store = TestRelayStore(database)
    app = FastAPI(title="RMA test relay", docs_url=None, redoc_url=None)

    def authorize(authorization: str | None = Header(default=None)) -> None:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="TEST_RELAY_UNAUTHORIZED")

    @app.get("/health")
    def health(_: None = Depends(authorize)) -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/records")
    def create_record(payload: RelayRecord, _: None = Depends(authorize)) -> dict[str, Any]:
        return store.create(payload)

    @app.post("/records/batch")
    def create_batch(payload: RelayBatch, _: None = Depends(authorize)) -> dict[str, Any]:
        return store.create_batch(payload)

    @app.post("/records/query")
    def query_records(payload: RelayQuery, _: None = Depends(authorize)) -> dict[str, Any]:
        return {"items": store.query(payload.source_request_ids)}

    @app.get("/records/{call_id}")
    def get_record(call_id: str, _: None = Depends(authorize)) -> dict[str, Any]:
        record = store.get(call_id)
        if record is None:
            raise HTTPException(status_code=404, detail="TEST_RELAY_RECORD_NOT_FOUND")
        return record

    @app.put("/control/default")
    def set_default(control: RelayControl, _: None = Depends(authorize)) -> dict[str, Any]:
        try:
            return store.configure(control)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.patch("/control/records/{call_id}")
    def update_record(
        call_id: str, control: RelayControl, _: None = Depends(authorize)
    ) -> dict[str, Any]:
        result = store.update(call_id, control)
        if result is None:
            raise HTTPException(status_code=404, detail="TEST_RELAY_RECORD_NOT_FOUND")
        return result

    @app.post("/control/reset")
    def reset(_: None = Depends(authorize)) -> dict[str, str]:
        store.reset()
        return {"status": "reset"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Loopback-only RMA relay simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    if args.host not in LOOPBACK_HOSTS:
        raise SystemExit("TEST_RELAY_LOOPBACK_BIND_REQUIRED")
    uvicorn.run(create_app(database=args.database, token=args.token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
