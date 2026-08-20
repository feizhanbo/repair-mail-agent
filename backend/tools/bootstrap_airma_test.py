from __future__ import annotations

import os
import argparse
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote_plus

import pymysql
import paramiko
from dotenv import dotenv_values
from sqlalchemy.engine import make_url
from sshtunnel import SSHTunnelForwarder


TARGET_DATABASE = "AIRMA_test"
SOURCE_DATABASE = "repair_system_test"
MASTER_TABLES = ("sn_assets", "board_cards", "customer_service_policies")
BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent


def _settings() -> dict[str, str]:
    values = {**dotenv_values(REPO_DIR / ".env"), **os.environ}
    return {key: str(value) for key, value in values.items() if value is not None}


def _target_url(*, username: str, password: str, port: int) -> str:
    return f"mysql+asyncmy://{quote_plus(username)}:{quote_plus(password)}@127.0.0.1:{port}/{TARGET_DATABASE}"


def _run(command: list[str], *, database_url: str, smoke: bool = False) -> None:
    environment = dict(os.environ)
    environment["DATABASE_URL"] = database_url
    environment["DB_NAME"] = TARGET_DATABASE
    environment["IMAP_FETCH_ENABLED"] = "false"
    environment["AUTO_SEND_ENABLED"] = "false"
    environment["RMA_AUTO_SEND_ENABLED"] = "false"
    if smoke:
        environment["DB_SMOKE_DATABASE_URL"] = database_url
    subprocess.run(command, cwd=BACKEND_DIR, env=environment, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema-only", action="store_true", help="Create AIRMA_test and run Alembic only")
    parser.add_argument("--recreate-schema-only", action="store_true", help="Recreate an empty partial AIRMA_test and run Alembic")
    parser.add_argument("--inspect-only", action="store_true", help="Read target revision and protected table counts only")
    parser.add_argument("--verify-only", action="store_true", help="Run read-only schema and seed verification")
    parser.add_argument(
        "--external-smoke",
        action="store_true",
        help="Run real text-model and reversible OSS smoke checks against AIRMA_test",
    )
    args = parser.parse_args()
    values = _settings()
    source_url = make_url(values["DATABASE_URL"])
    username = source_url.username or "root"
    password = source_url.password or values.get("MYSQL_ROOT_PASSWORD", "")
    # sshtunnel 0.4 still references the removed Paramiko DSSKey attribute.
    if not hasattr(paramiko, "DSSKey"):
        paramiko.DSSKey = paramiko.RSAKey  # type: ignore[attr-defined]
    tunnel = SSHTunnelForwarder(
        (values["SSH_HOST"], int(values.get("SSH_PORT", "22"))),
        ssh_username=values["SSH_USER"],
        ssh_password=values["SSH_PASSWORD"],
        remote_bind_address=(values.get("SSH_REMOTE_MYSQL_HOST", "127.0.0.1"), int(values.get("SSH_REMOTE_MYSQL_PORT", "3306"))),
        local_bind_address=("127.0.0.1", 0),
    )
    tunnel.start()
    try:
        connection = pymysql.connect(host="127.0.0.1", port=tunnel.local_bind_port, user=username, password=password, charset="utf8mb4", autocommit=True)
        with connection.cursor() as cursor:
            if args.inspect_only:
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
                    (TARGET_DATABASE,),
                )
                if not int(cursor.fetchone()[0]):
                    print({"database": TARGET_DATABASE, "exists": False})
                    return 0
                counts: dict[str, int] = {}
                for table in ("emails", "repair_tickets", "sn_assets", "board_cards", "customer_service_policies", "users", "workflow_statuses"):
                    cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_DATABASE}`.`{table}`")
                    counts[table] = int(cursor.fetchone()[0])
                cursor.execute(f"SELECT version_num FROM `{TARGET_DATABASE}`.alembic_version")
                revision = cursor.fetchone()[0]
                print({"database": TARGET_DATABASE, "exists": True, "revision": revision, "counts": counts})
                return 0
            if args.recreate_schema_only:
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
                    (TARGET_DATABASE,),
                )
                if int(cursor.fetchone()[0]):
                    protected_counts: dict[str, int] = {}
                    for protected_table in ("emails", "repair_tickets", "sn_assets", "board_cards", "users"):
                        cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_DATABASE}`.`{protected_table}`")
                        protected_counts[protected_table] = int(cursor.fetchone()[0])
                    protected_rows = sum(protected_counts.values())
                    if protected_rows:
                        raise RuntimeError(f"REFUSE_RECREATE_NONEMPTY_TARGET:{protected_counts}")
                    cursor.execute(f"DROP DATABASE `{TARGET_DATABASE}`")
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{TARGET_DATABASE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        connection.close()

        database_url = _target_url(username=username, password=password, port=tunnel.local_bind_port)
        if args.external_smoke:
            _run([sys.executable, "tools/run_external_preclassification_smoke.py"], database_url=database_url)
            return 0
        if args.verify_only:
            _run([sys.executable, "-m", "pytest", "-q", "tests/test_database_smoke.py"], database_url=database_url, smoke=True)
            connection = pymysql.connect(host="127.0.0.1", port=tunnel.local_bind_port, user=username, password=password, charset="utf8mb4")
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT version_num FROM `{TARGET_DATABASE}`.alembic_version")
                    revision = cursor.fetchone()[0]
                    counts: dict[str, int] = {}
                    for table in ("sn_assets", "board_cards", "customer_service_policies", "workflow_statuses", "workflow_transitions", "roles", "reply_templates", "emails", "repair_tickets"):
                        cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_DATABASE}`.`{table}`")
                        counts[table] = int(cursor.fetchone()[0])
                    cursor.execute(
                        "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
                        "AND ((TABLE_NAME='emails' AND COLUMN_NAME IN ('persistence_tier','classification_locked')) "
                        "OR (TABLE_NAME='ai_call_logs' AND COLUMN_NAME='mail_fetch_record_id') "
                        "OR (TABLE_NAME='ai_call_logs' AND COLUMN_NAME IN ('prompt_hash','route_name','route_attempt','fallback_used')) "
                        "OR (TABLE_NAME='mail_fetch_records' AND COLUMN_NAME IN ('processing_stage','thread_id','classification_evidence')))",
                        (TARGET_DATABASE,),
                    )
                    required_column_count = int(cursor.fetchone()[0])
                    if required_column_count != 10:
                        raise RuntimeError(f"PRECLASSIFICATION_SCHEMA_INCOMPLETE:{required_column_count}")
                    cursor.execute(
                        f"INSERT INTO `{TARGET_DATABASE}`.mail_fetch_records "
                        "(mailbox_account,folder_name,uid_validity,imap_uid,message_id,fetch_status,processing_stage,intent_type,handling_level,attempt_count) "
                        "VALUES ('airma-verify@example.test','INBOX',1,'verify-third','<verify-third@example.test>',"
                        "'classified_third','third_completed','invoice','lifecycle_only',1)"
                    )
                    cursor.execute(
                        f"SELECT COUNT(*) FROM `{TARGET_DATABASE}`.emails WHERE message_id='<verify-third@example.test>'"
                    )
                    if int(cursor.fetchone()[0]) != 0:
                        raise RuntimeError("THIRD_CREATED_EMAIL")
                    cursor.execute(
                        f"INSERT INTO `{TARGET_DATABASE}`.emails "
                        "(persistence_tier,classification_locked,mail_direction,mailbox_account,message_id,from_address,"
                        "parse_status,processing_stage,intent_type,handling_level,retryable) "
                        "VALUES ('minimal',0,'inbound','airma-verify@example.test','<verify-unknown@example.test>',"
                        "'customer@example.test','manual_review','minimal_persisted','unknown','unknown',0)"
                    )
                    verify_email_id = int(cursor.lastrowid)
                    cursor.execute(
                        f"INSERT INTO `{TARGET_DATABASE}`.email_attachments "
                        "(email_id,oss_object_id,file_name,content_type,file_size,parse_status) "
                        "VALUES (%s,NULL,'fault.png','image/png',123,'raw_eml_only')",
                        (verify_email_id,),
                    )
                    cursor.execute(
                        f"INSERT INTO `{TARGET_DATABASE}`.manual_review_tasks "
                        "(ticket_id,email_id,task_type,priority,status) VALUES (NULL,%s,'unknown_mail_classification','high','pending')",
                        (verify_email_id,),
                    )
                    cursor.execute(
                        f"SELECT COUNT(*) FROM `{TARGET_DATABASE}`.repair_tickets WHERE source_email_id=%s",
                        (verify_email_id,),
                    )
                    if int(cursor.fetchone()[0]) != 0:
                        raise RuntimeError("UNKNOWN_CREATED_TICKET")
                    connection.rollback()
            finally:
                connection.close()
            print({"database": TARGET_DATABASE, "revision": revision, "counts": counts, "required_column_count": required_column_count, "transactional_routing_checks": ["third_ledger_only", "unknown_minimal_no_ticket", "metadata_only_attachment"], "verification": "passed"})
            return 0
        _run([sys.executable, "-m", "alembic", "upgrade", "head"], database_url=database_url)
        if args.schema_only or args.recreate_schema_only:
            connection = pymysql.connect(host="127.0.0.1", port=tunnel.local_bind_port, user=username, password=password, charset="utf8mb4")
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT version_num FROM `{TARGET_DATABASE}`.alembic_version")
                    revision = cursor.fetchone()[0]
            finally:
                connection.close()
            print({"database": TARGET_DATABASE, "revision": revision, "mode": "schema_only"})
            return 0
        _run([sys.executable, "-m", "app.seed"], database_url=database_url)

        connection = pymysql.connect(host="127.0.0.1", port=tunnel.local_bind_port, user=username, password=password, charset="utf8mb4", autocommit=False)
        counts: dict[str, tuple[int, int]] = {}
        try:
            with connection.cursor() as cursor:
                for table in MASTER_TABLES:
                    cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_DATABASE}`.`{table}`")
                    existing_target_count = int(cursor.fetchone()[0])
                    if existing_target_count and table != "customer_service_policies":
                        raise RuntimeError(f"TARGET_MASTER_TABLE_NOT_EMPTY:{table}:{existing_target_count}")
                    cursor.execute(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                        (TARGET_DATABASE, table),
                    )
                    columns = [row[0] for row in cursor.fetchall() if row[0] not in {"id", "created_at", "updated_at", "imported_by_user_id"}]
                    quoted = ", ".join(f"`{column}`" for column in columns)
                    statement = (
                        f"INSERT INTO `{TARGET_DATABASE}`.`{table}` ({quoted}) "
                        f"SELECT {quoted} FROM `{SOURCE_DATABASE}`.`{table}`"
                    )
                    if table == "customer_service_policies":
                        updates = ", ".join(
                            f"`{column}`=VALUES(`{column}`)" for column in columns if column != "policy_code"
                        )
                        statement += f" ON DUPLICATE KEY UPDATE {updates}"
                    cursor.execute(statement)
                    cursor.execute(f"SELECT COUNT(*) FROM `{SOURCE_DATABASE}`.`{table}`")
                    source_count = int(cursor.fetchone()[0])
                    cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_DATABASE}`.`{table}`")
                    target_count = int(cursor.fetchone()[0])
                    if table != "customer_service_policies" and source_count != target_count:
                        raise RuntimeError(f"SEED_COUNT_MISMATCH:{table}:{source_count}:{target_count}")
                    if table == "customer_service_policies":
                        cursor.execute(
                            f"SELECT COUNT(*) FROM `{SOURCE_DATABASE}`.`{table}` AS source "
                            f"LEFT JOIN `{TARGET_DATABASE}`.`{table}` AS target ON target.policy_code=source.policy_code "
                            "WHERE target.id IS NULL"
                        )
                        missing_count = int(cursor.fetchone()[0])
                        if missing_count:
                            raise RuntimeError(f"SEED_POLICY_MISSING:{missing_count}")
                    counts[table] = (source_count, target_count)
                connection.commit()
                cursor.execute(f"SELECT version_num FROM `{TARGET_DATABASE}`.alembic_version")
                revision = cursor.fetchone()[0]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        print({"database": TARGET_DATABASE, "revision": revision, "master_data_counts": counts, "safety": "mail_and_auto_send_disabled_during_bootstrap"})
        return 0
    finally:
        tunnel.stop()


if __name__ == "__main__":
    raise SystemExit(main())
