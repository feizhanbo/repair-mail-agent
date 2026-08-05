from __future__ import annotations

import asyncio
from collections import defaultdict
from urllib.parse import unquote, urlparse

import asyncmy
from sqlalchemy.dialects import mysql as mysql_dialect

from app.config import settings
from app.models import Base

LOCAL_ALEMBIC_HEAD = "m0h5c6d7e8f9"
_mysql_d = mysql_dialect.dialect()


def parse_database_url(url: str):
    url = url.replace("mysql+asyncmy://", "mysql://")
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 3306
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    dbname = parsed.path.lstrip("/")
    return host, port, user, password, dbname


def _norm_remote_type(raw_type):
    t = str(raw_type).strip().lower()
    if " unsigned" in t:
        t = t.replace(" unsigned", "")
        t += " unsigned"
    return t


def _norm_default(val):
    if val is None:
        return None
    s = str(val).strip().strip("'\"")
    if s.lower() == "null":
        return None
    return s


def _get_orm_type(col):
    t = str(col.type.compile(dialect=_mysql_d)).strip().lower()
    if t == "bool":
        t = "tinyint(1)"
    if t == "integer":
        t = "int"
    t = t.replace(", ", ",")
    return t


def _get_orm_default(col):
    if col.server_default is None:
        return None
    sd = col.server_default
    if hasattr(sd, "arg"):
        arg = sd.arg
        if hasattr(arg, "text"):
            return arg.text
        return str(arg)
    return str(sd)


async def get_remote_schema(host, port, user, password, dbname):
    conn = await asyncmy.connect(host=host, port=port, user=user, password=password)
    try:
        await conn.execute(f"USE `{dbname}`")

        tables = {}
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
                (dbname,),
            )
            remote_table_names = {row[0] async for row in cursor}

        for tname in sorted(remote_table_names):
            cols = {}
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION",
                    (dbname, tname),
                )
                async for row in cursor:
                    col_name = row[0]
                    cols[col_name] = {
                        "type": row[1],
                        "nullable": row[2] == "YES",
                        "default": _norm_default(row[3]),
                        "auto_increment": "auto_increment" in (row[4] or ""),
                    }

            indexes = defaultdict(set)
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE "
                    "FROM information_schema.STATISTICS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
                    (dbname, tname),
                )
                async for row in cursor:
                    idx_name = row[0]
                    indexes[idx_name].add(row[1])

            fks = []
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                    "FROM information_schema.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "AND REFERENCED_TABLE_NAME IS NOT NULL",
                    (dbname, tname),
                )
                async for row in cursor:
                    fks.append({
                        "column": row[0],
                        "ref_table": row[1],
                        "ref_column": row[2],
                    })

            tables[tname] = {
                "columns": cols,
                "indexes": dict(indexes),
                "foreign_keys": fks,
            }

        alembic_version = None
        async with conn.cursor() as cursor:
            try:
                await cursor.execute("SELECT version_num FROM alembic_version")
                row = await cursor.fetchone()
                if row:
                    alembic_version = row[0]
            except Exception:
                pass

        business_emails_exists = False
        async with conn.cursor() as cursor:
            try:
                await cursor.execute(
                    "SELECT TABLE_NAME FROM information_schema.VIEWS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'business_emails'",
                    (dbname,),
                )
                row = await cursor.fetchone()
                business_emails_exists = row is not None
            except Exception:
                pass

        return {
            "tables": tables,
            "alembic_version": alembic_version,
            "business_emails": business_emails_exists,
        }
    finally:
        await conn.ensure_closed()


def get_orm_schema():
    orm_tables = {}
    for tname, table in Base.metadata.tables.items():
        cols = {}
        for cname, col in table.columns.items():
            cols[cname] = {
                "type": _get_orm_type(col),
                "nullable": col.nullable,
                "default": _get_orm_default(col),
                "auto_increment": getattr(col, "autoincrement", False),
            }

        indexes = {}
        for idx in table.indexes:
            idx_cols = {c.name for c in idx.columns}
            indexes[idx.name] = idx_cols

        for constraint in table.constraints:
            if constraint.__class__.__name__ == "UniqueConstraint":
                uc_cols = {c.name for c in constraint.columns}
                indexes[constraint.name] = uc_cols

        fks = []
        for cname, col in table.columns.items():
            for fk in col.foreign_keys:
                fks.append({
                    "column": cname,
                    "ref_table": fk.column.table.name,
                    "ref_column": fk.column.name,
                })

        orm_tables[tname] = {
            "columns": cols,
            "indexes": indexes,
            "foreign_keys": fks,
        }

    return orm_tables


def compare(remote_schema, orm_schema):
    remote_tables = set(remote_schema["tables"].keys())
    orm_tables = set(orm_schema.keys())

    only_remote = sorted(remote_tables - orm_tables)
    only_orm = sorted(orm_tables - remote_tables)
    common = sorted(remote_tables & orm_tables)

    differences = 0

    print("=" * 70)
    print("TABLE EXISTENCE")
    print("=" * 70)
    if only_orm:
        print(f"\nTables only in ORM ({len(only_orm)}):")
        for t in only_orm:
            print(f"  + {t}")
        differences += len(only_orm)
    else:
        print("\nTables only in ORM: (none)")
    if only_remote:
        print(f"\nTables only in remote ({len(only_remote)}):")
        for t in only_remote:
            print(f"  - {t}")
        differences += len(only_remote)
    else:
        print("\nTables only in remote: (none)")
    print(f"\nCommon tables: {len(common)}")

    print("\n" + "=" * 70)
    print("COLUMN DIFFERENCES")
    print("=" * 70)

    col_diff_count = 0
    for tname in common:
        r_cols = remote_schema["tables"][tname]["columns"]
        o_cols = orm_schema[tname]["columns"]
        r_set = set(r_cols.keys())
        o_set = set(o_cols.keys())
        all_cols = sorted(r_set | o_set)

        table_has_diff = False
        for cname in all_cols:
            r = r_cols.get(cname)
            o = o_cols.get(cname)

            if r is None:
                if not table_has_diff:
                    print(f"\n  [{tname}]")
                    table_has_diff = True
                print(f"    + {tname}.{cname} (only in ORM)")
                col_diff_count += 1
                continue
            if o is None:
                if not table_has_diff:
                    print(f"\n  [{tname}]")
                    table_has_diff = True
                print(f"    - {tname}.{cname} (only in remote)")
                col_diff_count += 1
                continue

            for attr in ("type", "nullable", "default", "auto_increment"):
                rv = r[attr]
                ov = o[attr]
                if attr == "type":
                    rv = _norm_remote_type(rv)
                if str(rv) != str(ov):
                    if not table_has_diff:
                        print(f"\n  [{tname}]")
                        table_has_diff = True
                    print(f"    ~ {tname}.{cname}.{attr}: remote={rv!r}  orm={ov!r}")
                    col_diff_count += 1

    if col_diff_count == 0:
        print("\n  (all columns consistent)")

    differences += col_diff_count

    print("\n" + "=" * 70)
    print("INDEX DIFFERENCES")
    print("=" * 70)

    idx_diff_count = 0
    for tname in common:
        r_idxs = remote_schema["tables"][tname]["indexes"]
        o_idxs = orm_schema[tname]["indexes"]

        r_names = {n for n in r_idxs if n != "PRIMARY"}
        o_names = set(o_idxs.keys())

        only_remote_idx = sorted(r_names - o_names)
        only_orm_idx = sorted(o_names - r_names)

        if only_remote_idx or only_orm_idx:
            print(f"\n  [{tname}]")
            for idx_name in only_remote_idx:
                print(f"    - {idx_name} (only in remote)")
                idx_diff_count += 1
            for idx_name in only_orm_idx:
                print(f"    + {idx_name} (only in ORM)")
                idx_diff_count += 1

    if idx_diff_count == 0:
        print("\n  (all indexes consistent)")

    differences += idx_diff_count

    print("\n" + "=" * 70)
    print("ALEMBIC VERSION")
    print("=" * 70)
    remote_ver = remote_schema["alembic_version"]
    print(f"  Remote : {remote_ver}")
    print(f"  Local  : {LOCAL_ALEMBIC_HEAD}")
    if remote_ver != LOCAL_ALEMBIC_HEAD:
        print("  Status : MISMATCH")
        differences += 1
    else:
        print("  Status : match")

    print("\n" + "=" * 70)
    print("VIEWS")
    print("=" * 70)
    print(f"  business_emails in remote: {remote_schema['business_emails']}")
    if not remote_schema["business_emails"]:
        differences += 1

    print("\n" + "=" * 70)
    if differences == 0:
        print("All consistent")
    else:
        print(f"{differences} differences found")


async def main():
    try:
        host, port, user, password, dbname = parse_database_url(settings.DATABASE_URL)
    except Exception:
        print("Failed to parse DATABASE_URL from app config.")
        return

    try:
        remote_schema = await get_remote_schema(host, port, user, password, dbname)
    except Exception:
        print(
            f"Failed to connect to remote database at {host}:{port}. "
            "Please check: 1) SSH tunnel is established "
            "2) Database credentials are correct "
            "3) Database server is running"
        )
        return

    orm_schema = get_orm_schema()
    compare(remote_schema, orm_schema)


if __name__ == "__main__":
    asyncio.run(main())
