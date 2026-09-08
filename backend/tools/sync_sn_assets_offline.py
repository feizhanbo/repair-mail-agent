#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQL Server dbo.oins_rma -> 系统 MySQL sn_assets 的离线批量（流式）同步脚本。

用途：在不启动 backend 定时调度的前提下，手工把 SQL Server
中转库（dbo.oins_rma，约 96.7 万行）全量拉取进业务库 sn_assets，按 SAP 行身份
(source_system, insID) 逐批 upsert（INSERT ... ON DUPLICATE KEY UPDATE），并推进
external_sync_checkpoints 的 'sqlserver_sn_assets' 记录（仅 apply 全量），使票据
SN 校验依赖的“SQL Server SN 快照新鲜度”门槛通过。

本重构版（sync-sn-assets-offline-stream）语义：
1. 流式读取源表（项目标准 pyodbc + fetchmany 分批、无 ORDER BY、无全表排序）；
2. 同步前拒绝源 `insID` 为空或重复；SN 重复是合法多行，不去重。全量 APPLY 在同一
   事务中将本轮未出现的历史 SQL Server 行软失效，并推进 checkpoint；
3. 通用可复用核心 stream_upsert_by_key()：按唯一键分批存在性预查（区分 would
   insert/update）后分批 executemany upsert；dry-run 与 apply 共用同一循环。

模式：
- DRY-RUN（默认，不给 --apply 时生效）：只做连接/表结构校验 + 与 apply 相同的分批
  存在性估算，不写任何持久数据，退出码 0；
- APPLY（--apply 无 --limit）：数据写入与 checkpoint 推进放同一事务，原子提交；
- APPLY --limit N：只处理前 N 个“非跳过”行后停止，正常提交自己的事务，但跳过
  checkpoint 推进（控制台会明确提示“子集试运行，未推进 checkpoint”）；
- DRY-RUN --limit N：只估算前 N 行。

字段策略（sn_assets 写入侧）：
- sn = str(internalSN).strip()；空/纯空白 -> 跳过该行；SN 是业务查询键且允许重复；
- customer_code / material_code 为空 -> ''（NOT NULL 列）；customer_name / material_name
  为空 -> NULL；ins_id 解析为 int（失败为 NULL）；warranty_end_date 解析为 date（可空）；
- parent_sn / top_sn / parent_material_code / top_material_code 保留源值字符串（空 -> NULL）；
- 源未映射的本地列一律 NULL：service_tracking_card_no / warranty_start_date /
  source_file_name / source_file_hash / source_row_no / source_updated_at /
  source_row_hash / imported_by_user_id；`external_id` 固定为 `str(insID)`；
- 常量：asset_status='valid'、source_system='sqlserver'；
- raw_data = JSON {source:sqlserver, tool:sync_sn_assets_offline, run_id:<runid>}，
  runid 形如 snoff-<epoch>-<pid>；imported_at / created_at（仅插入） / updated_at
  由 SQL 端 NOW() 写入；ON DUPLICATE KEY UPDATE 覆盖除行身份与 created_at 外的
  所有列。

编码硬规则（本文件任何地方都不得违反）：
- 全部数据值一律经 %s 参数绑定，绝不拼接进 SQL；
- 目标连接 pymysql 必须显式 charset='utf8mb4'；不做任何跨库/跨排序规则的比较
  （不建 TEMPORARY 表、不做 collation 转换），故不存在此类风险点。

复用指引（通用核心 stream_upsert_by_key，最小示例见其 docstring）：
  其它“源表 -> 目标表按唯一键 upsert”的同步只需提供：源 SELECT（固定标识符）、
  目标表名与唯一键列名、写入列元组、一次性构建好的 upsert SQL、以及把“源行元组”
  变成“(唯一键, VALUES 参数元组)”的转换函数；逐批读取/存在性预查/计数由核心完成。

禁止事项：
- 本脚本是离线手工工具，禁止与后端定时 SN 同步并发执行（同一批 sn_assets 双写会
  互相污染）；
- 密码仅来自进程环境变量或项目根 `.env`；源连接优先兼容 MSSQL_*，并支持项目现行
  RELAY_SQLSERVER_*，目标连接使用 DATABASE_URL
  （目标 MySQL）；所有错误输出统一把两处密码掩码为 ***；
- 依赖 pyodbc / pymysql 为惰性导入，未安装时本模块仍可 import / py_compile，
  运行到对应步骤才报错（exit 2）；
- 退出码：0=成功 / 1=运行失败（连接、校验、读写等）/ 2=配置错误（缺环境变量、
  端口非法、白名单外目标库、列映射参数非法、依赖缺失）。
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, date, time as dt_time
from decimal import Decimal
from urllib.parse import quote, unquote, urlsplit

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# tools/ 位于 backend/ 下；项目 .env 位于 backend/ 的上一级。
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
REPO_ROOT_DIR = os.path.dirname(BACKEND_DIR)

REQUIRED_VARS = [
    "MSSQL_HOST",
    "MSSQL_PORT",
    "MSSQL_USER",
    "MSSQL_PASSWORD",
    "MSSQL_DATABASE",
]
SOURCE_ENV_ALIASES = {
    "MSSQL_HOST": "RELAY_SQLSERVER_HOST",
    "MSSQL_PORT": "RELAY_SQLSERVER_PORT",
    "MSSQL_USER": "RELAY_SQLSERVER_USER",
    "MSSQL_PASSWORD": "RELAY_SQLSERVER_PASSWORD",
    "MSSQL_DATABASE": "RELAY_SQLSERVER_DATABASE",
}

DEFAULT_TIMEOUT = 10
TDS_VERSION = "7.3"
APP_NAME = "sync-sn-assets-offline"

# ---------------------------------------------------------------------------
# 源库常量（维护点：与 backend/app/config.py 保持一致）
#   RELAY_SQLSERVER_SN_SCHEMA = "dbo"
#   RELAY_SQLSERVER_SN_TABLE  = "oins_rma"
# ---------------------------------------------------------------------------
RELAY_SN_SCHEMA = "dbo"
RELAY_SN_TABLE = "oins_rma"

# 镜像 backend/app/config.py 的 RELAY_SQLSERVER_SN_COLUMN_MAP（11 对本地字段 -> 源列名）。
# 维护点：后端映射一旦变更，此处必须同步修改，否则列校验/读取会失配。
DEFAULT_SN_COLUMN_MAP = {
    "ins_id": "insID",
    "sn": "internalSN",
    "customer_code": "customer",
    "customer_name": "custmrName",
    "material_code": "ITEMCODE",
    "material_name": "ITEMNAME",
    "parent_material_code": "U_FatherItem",
    "parent_sn": "U_FatherSerialNum",
    "top_material_code": "U_topitemcode",
    "top_sn": "U_TOPSN",
    "warranty_end_date": "ExpDate",
}

# 本地字段的固定顺序（与后端 SnAsset 语义一致）；源 SELECT 与行解析都按此顺序进行，
# --sn-column-map 覆盖只允许改“源列名”，不允许增删本地字段。
SN_LOCAL_FIELDS = (
    "ins_id",
    "sn",
    "customer_code",
    "customer_name",
    "material_code",
    "material_name",
    "parent_material_code",
    "parent_sn",
    "top_material_code",
    "top_sn",
    "warranty_end_date",
)

# ---------------------------------------------------------------------------
# 批大小 / 进度 / checkpoint / 白名单常量
# ---------------------------------------------------------------------------
FETCH_BATCH = 20000       # 源流式读取批大小（内存与源行数无关）
WRITE_BATCH = 5000        # 目标 upsert / 存在性分组批大小（控制单条 SQL 体积）
EXISTENCE_BATCH = 500     # 存在性预查 IN(...) 的切片大小（限制单条 SQL 参数个数）
PROGRESS_INTERVAL = 100000  # 每读多少行打印一次进度

CHECKPOINT_NAME = "sqlserver_sn_assets"  # 与 backend/app/services/sap_sn_sync.py 一致
# 目标库名白名单：镜像 backend/app/config.py 的 DESTRUCTIVE_TEST_DATABASE_ALLOWLIST
ALLOWED_TARGET_DBS = ["repair_system_test", "AIRMA_test"]


# ---------------------------------------------------------------------------
# 目标 sn_assets 写入列定义（维护点：与 backend/app/models/master_data.py 的
# SnAsset 保持一致；SnAsset 的 id 由库自增，不在写入列内）
# ---------------------------------------------------------------------------
# 三个时间戳列在 SQL 中固定为数据库 NOW()（不用 %s 参数，见 _build_sn_assets_upsert_sql）。
SN_ASSETS_SQL_EXPR = {
    "imported_at": "NOW()",
    "created_at": "NOW()",
    "updated_at": "NOW()",
}
# ON DUPLICATE KEY UPDATE 时 created_at 仅插入时写。
SN_ASSETS_UPDATE_SKIP = frozenset(["created_at"])

# sn_assets 写入列全集（顺序固定；其中的 %s 参数列见 SN_ASSETS_PARAM_COLUMNS）。
SN_ASSETS_WRITE_COLUMNS = (
    "ins_id",
    "customer_code",
    "customer_name",
    "material_code",
    "material_name",
    "sn",
    "service_tracking_card_no",
    "parent_sn",
    "top_sn",
    "parent_material_code",
    "top_material_code",
    "asset_status",
    "warranty_start_date",
    "warranty_end_date",
    "source_file_name",
    "source_file_hash",
    "source_row_no",
    "raw_data",
    "source_system",
    "external_id",
    "source_updated_at",
    "source_row_hash",
    "imported_by_user_id",
    "imported_at",
    "created_at",
    "updated_at",
)
# 真正以 %s 参数绑定写入的列（排除 SQL 表达式列）；row_to_write 返回值顺序与此一致。
SN_ASSETS_PARAM_COLUMNS = tuple(
    c for c in SN_ASSETS_WRITE_COLUMNS if c not in SN_ASSETS_SQL_EXPR
)
# 目标表名与唯一键（均为固定常量，不出现在任何参数值里）
SN_ASSETS_TABLE = "sn_assets"
SN_ASSETS_UNIQUE_COL = "ins_id"


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
def load_env():
    """加载项目根 `.env`，并兼容可选的 backend/.env。

    python-dotenv 未安装时退化为仅使用进程已有的 os.environ，不阻断脚本；
    override=False：已存在的进程环境变量优先，两份文件都不覆盖它。
    """
    try:
        from dotenv import load_dotenv  # 惰性导入
    except ImportError:
        return
    for env_path in (os.path.join(REPO_ROOT_DIR, ".env"), os.path.join(BACKEND_DIR, ".env")):
        try:
            load_dotenv(env_path, override=False)
        except Exception:
            pass


def read_config():
    """读取并校验源库（MSSQL_*）配置。

    缺失必填项时打印中文提示并返回 None（由调用方返回 exit 2）。
    目标 MySQL 的 DATABASE_URL 在 main 中单独校验（来源是仓库根 .env）。
    """
    load_env()
    config = {}
    missing = []
    for key in REQUIRED_VARS:
        val = os.environ.get(key, "").strip()
        if not val:
            val = os.environ.get(SOURCE_ENV_ALIASES[key], "").strip()
        if not val:
            missing.append(key)
        config[key] = val
    if missing:
        print("[配置错误] 以下必填环境变量未设置：")
        for k in missing:
            print("  - {}".format(k))
        print("请通过进程环境变量或项目根 .env 提供 MSSQL_* / RELAY_SQLSERVER_*。")
        return None
    return config


def _parse_mysql_url(url):
    """解析 mysql+asyncmy://user:pass@host:port/dbname 形态的 DATABASE_URL。

    兼容 mysql:// 与任意 mysql+xxx:// 前缀；用户名/密码/库名做 URL 解码。
    解析失败抛 ValueError（中文提示，由调用方按配置错误处理）。
    """
    text = (url or "").strip()
    if not text or "://" not in text:
        raise ValueError("DATABASE_URL 格式不正确（缺少 '://'）")
    parts = urlsplit(text)
    if parts.scheme.split("+", 1)[0] != "mysql":
        raise ValueError("DATABASE_URL 非 MySQL 协议: {}".format(parts.scheme))
    if not parts.hostname:
        raise ValueError("DATABASE_URL 缺少主机名")
    try:
        port = parts.port or 3306
    except ValueError as e:
        raise ValueError("DATABASE_URL 端口无效: {}".format(e))
    dbname = unquote((parts.path or "").lstrip("/").split("?", 1)[0].split("/", 1)[0])
    return {
        "host": parts.hostname,
        "port": int(port),
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "database": dbname,
    }


# ---------------------------------------------------------------------------
# 密码掩码（绝不打印真实密码；MSSQL 密码与 DATABASE_URL 中密码都要掩）
# ---------------------------------------------------------------------------
def _mask_password(text, password):
    """把文本中出现的 password 替换为 ***，绝不打印真实密码。"""
    if not text:
        return text
    if password:
        try:
            if password in text:
                text = text.replace(password, "***")
        except Exception:
            pass
    return text


def _mask_db_password(text, db_password):
    """掩码 DATABASE_URL 中的密码：同时处理解码形态与 URL 编码形态。"""
    if not db_password:
        return text
    masked = _mask_password(text, db_password)
    try:
        masked = _mask_password(masked, quote(db_password, safe=""))
    except Exception:
        pass
    return masked


def _mask_all(text, mssql_password, db_password):
    """统一掩码两处密码后再返回。"""
    if db_password:
        text = _mask_db_password(text, db_password)
    return _mask_password(text, mssql_password)


# ---------------------------------------------------------------------------
# 值转换（与 sync_rma_data.py / sync_oins_rma_data.py 同实现，模块内私有复制）
# ---------------------------------------------------------------------------
def _ensure_str(v):
    """bytes -> str（兼容驱动返回 bytes 的情况），其余原样返回。"""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v


def _fix_gbk_str(v):
    """修复 SQL Server（代码页 936/GBK 排序规则）VARCHAR 列中文乱码。

    某些旧 SQL Server 驱动组合会把 GBK VARCHAR 按 latin-1 解码成乱码 str。本函数：
    - 非 str 或纯 ASCII -> 原样返回；
    - 含非 latin-1 字符（已是正确的 unicode 文本，如 nvarchar 中文）-> 原样返回；
    - 可 latin-1 编码的乱码 str -> 按 GBK 重解码；解码失败则原样返回。
    """
    if not isinstance(v, str) or v.isascii():
        return v
    try:
        raw = v.encode("latin-1")
    except UnicodeEncodeError:
        return v
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return v


def _json_default(o):
    """把任意值转为 JSON 可序列化类型。

    None/int/bool/float 原样返回；bytes -> utf-8 str(errors=replace)；
    datetime/date/time -> isoformat；Decimal -> str；其余 -> str。
    """
    if o is None or isinstance(o, (bool, int, float)):
        return o
    if isinstance(o, bytes):
        return o.decode("utf-8", errors="replace")
    if isinstance(o, (datetime, date, dt_time)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    return str(o)


def _to_int_or_none(v):
    """ins_id 数值化：可转 int 则返回 int，否则 None（不可解析视为无该外键）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v) if v else 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_date(v):
    """把质保期值统一为 date 或 None。

    date/datetime 直接取 date；字符串取前 10 位按 'YYYY-MM-DD' 解析；
    空串/不可解析视为“无质保期”（与后端 _warranty_date 语义一致）。
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    head = s[:10]
    if len(head) < 10 or head[4:5] != "-":
        return None
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


def _text_or_none(v):
    """可空文本列：None/纯空白 -> None；否则返回原样 str（不做清洗，质量不在此把关）。"""
    if v is None:
        return None
    s = str(v)
    if not s.strip():
        return None
    return s


def _text_or_empty(v):
    """NOT NULL 文本列：None/纯空白 -> ''；否则返回原样 str。"""
    if v is None:
        return ""
    s = str(v)
    if not s.strip():
        return ""
    return s


# ---------------------------------------------------------------------------
# 数据库连接与表结构校验
# ---------------------------------------------------------------------------
def _connect_mssql(host, port, user, password, database, timeout=DEFAULT_TIMEOUT):
    """使用项目标准 pyodbc 驱动建立 SQL Server 只读连接。

    源库在本脚本中只执行 SELECT（只读语义），从不写源库。
    """
    import pyodbc  # 惰性导入（项目 requirements 已固定版本）

    driver = os.environ.get("RELAY_SQLSERVER_DRIVER", "ODBC Driver 18 for SQL Server")
    encrypt = os.environ.get("RELAY_SQLSERVER_ENCRYPT", "true").strip().lower() in {"1", "true", "yes", "on"}
    trust = os.environ.get("RELAY_SQLSERVER_TRUST_SERVER_CERTIFICATE", "true").strip().lower() in {"1", "true", "yes", "on"}
    connection_string = (
        "DRIVER={{{0}}};SERVER={1},{2};DATABASE={3};UID={4};PWD={5};"
        "Encrypt={6};TrustServerCertificate={7};APP={8};"
    ).format(
        driver, host, port, database, user, password,
        "yes" if encrypt else "no", "yes" if trust else "no", APP_NAME,
    )
    return pyodbc.connect(connection_string, timeout=timeout, autocommit=False)


def _connect_mysql(db_info, database, timeout=DEFAULT_TIMEOUT):
    """建立目标 MySQL 的 pymysql 同步连接（单事务、显式 utf8mb4）。"""
    import pymysql  # 惰性导入（调用方已确认可用）
    return pymysql.connect(
        host=db_info["host"],
        port=db_info["port"],
        user=db_info["user"],
        password=db_info["password"],
        database=database,
        # 编码硬规则：目标连接必须显式 utf8mb4，保证中文/特殊字符可参数化写入
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=timeout,
    )


def _check_source_table(conn, expected_schema, table_name, column_map):
    """校验源表存在、schema 与预期一致、全部映射源列存在（值参数化，中文报错）。

    仅 SELECT INFORMATION_SCHEMA，不写源库；列名存在性按大小写不敏感比较
    （SELECT 中 [] 引用的列名对大小写同样不敏感，两者口径一致）。
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT TABLE_SCHEMA FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME=?",
            table_name,
        )
        schemas = [_ensure_str(r[0]) for r in cur.fetchall()]
    finally:
        try:
            cur.close()
        except Exception:
            pass
    if not schemas:
        raise ValueError(
            "源表不存在: [{0}].[{1}]（INFORMATION_SCHEMA.TABLES 中未找到）".format(
                expected_schema, table_name
            )
        )
    if expected_schema.lower() not in [s.lower() for s in schemas]:
        raise ValueError(
            "源表 schema 与预期不一致: 预期 [{0}]，实际 [{1}]".format(
                expected_schema, ", ".join(schemas)
            )
        )

    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=? AND TABLE_NAME=?",
            expected_schema, table_name,
        )
        existing = {_ensure_str(r[0]).upper() for r in cur.fetchall()}
    finally:
        try:
            cur.close()
        except Exception:
            pass
    missing = [c for c in column_map.values() if c.upper() not in existing]
    if missing:
        raise ValueError(
            "源表 [{0}].[{1}] 缺少映射源列: {2}（请核对列映射与源表结构）".format(
                expected_schema, table_name, ", ".join(missing)
            )
        )


def _check_source_ins_id_identity(conn, expected_schema, table_name, ins_id_column):
    """确认 oins_rma 的 insID 非空且行级唯一。"""
    quoted = "[{0}]".format(ins_id_column.replace("]", "]]"))
    table = "[{0}].[{1}]".format(expected_schema, table_name)
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM {0} WHERE {1} IS NULL".format(table, quoted))
        if int(cur.fetchone()[0] or 0):
            raise RuntimeError("SOURCE_INS_ID_MISSING")
        cur.execute(
            "SELECT TOP 1 {0} FROM {1} GROUP BY {0} HAVING COUNT(*) > 1".format(
                quoted, table
            )
        )
        if cur.fetchone() is not None:
            raise RuntimeError("SOURCE_INS_ID_DUPLICATE")
    finally:
        cur.close()


def _check_target_sn_unique_key(mysql_conn, target_db):
    """检查目标 sn_assets 是否存在 SQL Server 行身份唯一索引。"""
    sql = (
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
        "AND INDEX_NAME=%s AND NON_UNIQUE=0"
    )
    cur = mysql_conn.cursor()
    try:
        cur.execute(sql, (target_db, SN_ASSETS_TABLE, "uk_sn_assets_source_ins_id"))
        row = cur.fetchone()
        found = bool(row) and int(row[0]) > 0
    finally:
        try:
            cur.close()
        except Exception:
            pass
    if found:
        print("[检查] 目标唯一键 uk_sn_assets_source_ins_id(source_system,ins_id) 存在")
    else:
        print(
            "[警告] 目标库 {0} 未找到唯一索引 uk_sn_assets_source_ins_id；"
            "同步已停止，请先执行 Alembic migration。".format(target_db)
        )
    return found


# ---------------------------------------------------------------------------
# 目标 upsert SQL 构建（一次性构建，按 write_columns 生成；所有数据值走 %s）
# ---------------------------------------------------------------------------
def _build_sn_assets_upsert_sql():
    """构建 sn_assets 的 INSERT ... ON DUPLICATE KEY UPDATE SQL（只构建一次）。

    - 数据列全部以 %s 占位（参数顺序 = SN_ASSETS_PARAM_COLUMNS，即写列去掉三个
      SQL 表达式列）；imported_at/created_at/updated_at 固定为 NOW()（数据库时钟），
      不进参数（编码硬规则：外部数据一律 %s，绝不拼接）；
    - ON DUPLICATE KEY UPDATE 覆盖除 `source_system + ins_id` 行身份与 created_at（仅插入）
      外的全部列；updated_at = VALUES(updated_at) 即 NOW()。
    """
    value_specs = []
    update_specs = []
    for col in SN_ASSETS_WRITE_COLUMNS:
        if col in SN_ASSETS_SQL_EXPR:
            value_specs.append(SN_ASSETS_SQL_EXPR[col])
        else:
            value_specs.append("%s")
        if col not in {"ins_id", "source_system"} and col not in SN_ASSETS_UPDATE_SKIP:
            update_specs.append("`{0}` = VALUES(`{0}`)".format(col))
    sql = "INSERT INTO `{0}` ({1}) VALUES ({2}) ON DUPLICATE KEY UPDATE {3}".format(
        SN_ASSETS_TABLE,
        ", ".join("`{0}`".format(c) for c in SN_ASSETS_WRITE_COLUMNS),
        ", ".join(value_specs),
        ", ".join(update_specs),
    )
    return sql


def _build_src_select_sql(source_cols):
    """按固定 schema/表名与映射源列构造源 SELECT（无 ORDER BY，流式读取）。"""
    quoted = ", ".join("[{0}]".format(c.replace("]", "]]")) for c in source_cols)
    return "SELECT {0} FROM [{1}].[{2}]".format(quoted, RELAY_SN_SCHEMA, RELAY_SN_TABLE)


def _make_row_to_write(runid):
    """构建 row_to_write：源行元组 -> (insID 键, VALUES 参数元组)。

    源行元组顺序 = SN_LOCAL_FIELDS（与 SELECT 列顺序一致）；逐值做 bytes 解码 /
    GBK 修复 / 类型转换（见模块 docstring 字段策略）。参数元组顺序与
    SN_ASSETS_PARAM_COLUMNS 一一对应，靠 cell 字典按列名取值，避免手工排序错位。
    """
    raw_data_json = json.dumps(
        {"source": "sqlserver", "tool": APP_NAME, "run_id": runid},
        ensure_ascii=False,
        default=_json_default,
    )
    # customer_code / material_code 为 NOT NULL 列：空 -> ''
    blank_as_empty = frozenset(["customer_code", "material_code"])

    def row_to_write(raw):
        parsed = {}
        for field, raw_value in zip(SN_LOCAL_FIELDS, raw):
            v = _ensure_str(raw_value)
            v = _fix_gbk_str(v)
            if field == "ins_id":
                parsed[field] = _to_int_or_none(v)
            elif field == "warranty_end_date":
                parsed[field] = _to_date(v)
            elif field in blank_as_empty:
                parsed[field] = _text_or_empty(_json_default(v))
            else:
                parsed[field] = _text_or_none(_json_default(v))
        sn = parsed.get("sn")
        sn = "" if sn is None else str(sn).strip()
        if not sn or parsed.get("ins_id") is None:
            # SN 是业务查询键，insID 是同步行身份；任一缺失均隔离。
            return None

        # 源未映射的本地列一律 NULL；asset_status/source_system 固定常量；
        # imported_at/created_at/updated_at 由 SQL 端 NOW() 写入，不在此列
        cell = {
            "ins_id": parsed["ins_id"],
            "customer_code": parsed["customer_code"],
            "customer_name": parsed["customer_name"],
            "material_code": parsed["material_code"],
            "material_name": parsed["material_name"],
            "sn": sn,
            "service_tracking_card_no": None,   # 源无此列 -> NULL
            "parent_sn": parsed["parent_sn"],
            "top_sn": parsed["top_sn"],
            "parent_material_code": parsed["parent_material_code"],
            "top_material_code": parsed["top_material_code"],
            "asset_status": "valid",            # 常量
            "warranty_start_date": None,        # 源无此列 -> NULL
            "warranty_end_date": parsed["warranty_end_date"],
            "source_file_name": None,           # 源无此列 -> NULL
            "source_file_hash": None,
            "source_row_no": None,
            "raw_data": raw_data_json,          # JSON 文本以参数写入 JSON 列
            "source_system": "sqlserver",       # 常量
            "external_id": str(parsed["ins_id"]),
            "source_updated_at": None,
            "source_row_hash": None,
            "imported_by_user_id": None,
        }
        values = tuple(cell[c] for c in SN_ASSETS_PARAM_COLUMNS)
        return (parsed["ins_id"], values)

    return row_to_write


# ---------------------------------------------------------------------------
# 通用可复用核心：按唯一键把源游标流式 upsert 进目标表
# ---------------------------------------------------------------------------
def _fetch_existing_keys(dst_conn, exist_sql_head, keys):
    """执行分批存在性预查，返回“目标表里已存在”的唯一键集合。

    SQL 形如：SELECT <unique_col> FROM <table> WHERE <unique_col> IN (%s, ...)，
    按 EXISTENCE_BATCH 切片执行以限制单条 SQL 参数个数；键值全部 %s 参数绑定。
    """
    cur = dst_conn.cursor()
    existing = set()
    try:
        for i in range(0, len(keys), EXISTENCE_BATCH):
            part = keys[i:i + EXISTENCE_BATCH]
            sql = "{0} ({1})".format(exist_sql_head, ", ".join(["%s"] * len(part)))
            cur.execute(sql, part)
            for row in cur.fetchall():
                existing.add(row[0])
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return existing


def stream_upsert_by_key(
    src_cur,
    dst_conn,
    *,
    src_select_sql,
    dst_table,
    dst_unique_col,
    write_columns,
    upsert_sql,
    row_to_write,
    sql_expr_columns=None,
    update_skip_columns=None,
    dst_where_sql=None,
    write_batch=WRITE_BATCH,
    dry_run=False,
    max_processed=None,
    progress_label=None,
    on_row=None,
):
    """通用可复用核心：把源游标按唯一键分批流式 upsert 到目标表（内存与行数无关）。

    语义：
    - 无全表排序、无 TEMPORARY 表、无跨库/跨排序规则比较；源按 FETCH_BATCH 分批
      fetchmany，目标按 write_batch 攒批 executemany；
    - 每个批次先做存在性预查（SELECT <唯一键> FROM <目标表> WHERE <唯一键> IN
      (...)），按 EXISTENCE_BATCH 切片，区分 inserted（库里没有的键）/ updated
      （库里已有的键）——dry-run 与 apply 共用同一预查，计数口径一致；
    - dry_run=True 时只做预查与计数，不 executemany（不写任何持久数据）；
    - max_processed=N 时只处理前 N 个“非跳过”行后停止（--limit 子集试运行）；
    - 编码硬规则：所有数据值一律 %s 参数绑定；dst_table/dst_unique_col/write_columns
      等标识符必须来自固定常量（复用方不得拼接外部输入）。

    参数：
      src_select_sql   源 SELECT（调用方按固定标识符构造，本函数负责 execute）。
      dst_table        目标表名（固定常量）。
      dst_unique_col   目标唯一键列名（固定常量）。
      write_columns    写入列名元组（顺序即 INSERT 列顺序）。
      upsert_sql       调用方按 write_columns 一次性构建的
                       INSERT ... ON DUPLICATE KEY UPDATE SQL。
      row_to_write     callable(源行元组) -> (key, values 元组) 或 None；
                       None 表示跳过该行（计数进 skipped）；values 顺序须与
                       upsert_sql 中的 %s 参数列一一对应（不含 sql_expr_columns）。
      sql_expr_columns {列名: SQL 表达式}：这些列不进 %s 参数
                       （例如 sn_assets 的 {'updated_at': 'NOW()', ...}）。
      update_skip_columns ODKU 时除 dst_unique_col 外仍不覆盖的列集合
                       （例如 sn_assets 的 {'created_at'}）。
      write_batch      每组 upsert/存在性预查的行数（默认 500）。
      dry_run          只估算不写入（复用同一循环，保证两种模式行为一致）。
      max_processed    只处理前 N 个非跳过行后停止（None=不限）。
      progress_label   非空时每 PROGRESS_INTERVAL 行打印一次“已读”进度。
      on_row           可选钩子 callable(item)，item 为 row_to_write 的返回值
                       （含 None 跳过行），供调用方做额外逐行记账。

    返回 dict：read_count（已读取并计入的行，= skipped + processed）/
      skipped / processed / inserted / updated（后两者来自写入前存在性预查的估算，
      写入与估算共用同一预查）。

    复用到其他表的示例（最小形态）：
      # upsert_sql = "INSERT INTO t2 (id,name) VALUES (%s,%s) "
      #              "ON DUPLICATE KEY UPDATE name=VALUES(name)"
      counts = stream_upsert_by_key(
          src_cur, dst_conn,
          src_select_sql="SELECT id, name FROM dbo.t1",
          dst_table="t2", dst_unique_col="id",
          write_columns=("id", "name"),
          upsert_sql=upsert_sql,
          row_to_write=lambda raw: (str(raw[0]), (str(raw[0]), raw[1])),
          dry_run=True,
      )
    """
    expr_cols = dict(sql_expr_columns or {})
    param_columns = [c for c in write_columns if c not in expr_cols]
    identity_filter = (dst_where_sql + " AND ") if dst_where_sql else ""
    exist_sql_head = "SELECT `{0}` FROM `{1}` WHERE {2}`{0}` IN".format(
        dst_unique_col, dst_table, identity_filter
    )

    def flush_batch(items):
        """处理一批：存在性预查分类 -> （非 dry-run 时）executemany upsert。"""
        keys = [k for k, _v in items]
        existing = _fetch_existing_keys(dst_conn, exist_sql_head, keys)
        ins = upd = 0
        for k in keys:
            if k in existing:
                upd += 1
            else:
                ins += 1
        if not dry_run and items:
            upsert_cur.executemany(upsert_sql, [v for _k, v in items])
        return ins, upd

    counts = {"read_count": 0, "skipped": 0, "processed": 0, "inserted": 0, "updated": 0}
    expected_params = None
    pending = []
    upsert_cur = None
    stop = False
    try:
        src_cur.execute(src_select_sql)
        if not dry_run:
            upsert_cur = dst_conn.cursor()
        while True:
            raw_rows = src_cur.fetchmany(FETCH_BATCH)
            if not raw_rows:
                break
            for raw in raw_rows:
                item = row_to_write(raw)
                if item is None:
                    counts["skipped"] += 1
                    counts["read_count"] += 1
                    if on_row is not None:
                        on_row(item)
                    continue
                if max_processed is not None and counts["processed"] >= max_processed:
                    # 已达到子集上限：该行不再计入，停止读取后续行
                    stop = True
                    break
                key, values = item
                if expected_params is None:
                    expected_params = len(param_columns)
                    if len(values) != expected_params:
                        raise ValueError(
                            "row_to_write 返回参数个数 {0} 与 upsert_sql 的 %s 参数列数"
                            " {1} 不一致（请核对 write_columns / row_to_write）".format(
                                len(values), expected_params
                            )
                        )
                counts["processed"] += 1
                counts["read_count"] += 1
                pending.append((key, values))
                if len(pending) >= write_batch:
                    ins, upd = flush_batch(pending)
                    counts["inserted"] += ins
                    counts["updated"] += upd
                    pending = []
                if (
                    progress_label is not None
                    and counts["read_count"] % PROGRESS_INTERVAL == 0
                ):
                    print("[{0}] 已读 {1} 行...".format(progress_label, counts["read_count"]))
                if on_row is not None:
                    on_row(item)
            if stop:
                break
        if pending:
            ins, upd = flush_batch(pending)
            counts["inserted"] += ins
            counts["updated"] += upd
            pending = []
        return counts
    finally:
        try:
            src_cur.close()
        except Exception:
            pass
        if upsert_cur is not None:
            try:
                upsert_cur.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# checkpoint（仅 apply 全量；与数据同一事务，原子提交）
# ---------------------------------------------------------------------------
def _apply_checkpoint(mysql_conn, runid, counts):
    """upsert external_sync_checkpoints 的 'sqlserver_sn_assets' 记录。

    选择：与数据写入放同一事务、由调用方统一 COMMIT —— checkpoint 失败则整体回滚，
    绝不出现“数据已写但 checkpoint 未推进”的半成功状态。所有值 %s 参数绑定。
    """
    statistics_json = json.dumps(
        {
            "run_id": runid,
            "tool": APP_NAME,
            "read_count": counts["read_count"],
            "skipped": counts["skipped"],
            "inserted": counts["inserted"],
            "updated": counts["updated"],
        },
        ensure_ascii=False,
        default=_json_default,
    )
    sql = (
        "INSERT INTO external_sync_checkpoints "
        "(sync_name, cursor_value, last_full_sync_at, last_success_at, last_status, "
        "last_error_code, statistics_json, created_at, updated_at) "
        "VALUES (%s, NULL, NOW(), NOW(), 'succeeded', NULL, %s, NOW(), NOW()) "
        "ON DUPLICATE KEY UPDATE "
        "cursor_value = NULL, last_full_sync_at = NOW(), last_success_at = NOW(), "
        "last_status = 'succeeded', last_error_code = NULL, "
        "statistics_json = %s, updated_at = NOW()"
    )
    cur = mysql_conn.cursor()
    try:
        cur.execute(sql, (CHECKPOINT_NAME, statistics_json, statistics_json))
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _invalidate_missing_sqlserver_rows(mysql_conn, runid):
    """全量同步时失效本次源快照未触达的历史 SQL Server 行。"""
    cur = mysql_conn.cursor()
    try:
        cur.execute(
            "UPDATE sn_assets SET asset_status='invalid', updated_at=NOW() "
            "WHERE source_system='sqlserver' AND asset_status<>'invalid' "
            "AND (raw_data IS NULL OR JSON_UNQUOTE(JSON_EXTRACT(raw_data, '$.run_id')) <> %s)",
            (runid,),
        )
        return int(cur.rowcount or 0)
    finally:
        try:
            cur.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 连接收尾
# ---------------------------------------------------------------------------
def _rollback_safe(conn):
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass


def _close_safe(conn):
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="SQL Server oins_rma -> MySQL sn_assets 离线批量流式同步"
                    "（默认 DRY-RUN；按 insID 校验并幂等写入）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DRY-RUN 模式：只校验 + 存在性估算，不写任何持久数据（不给 --apply 时默认）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="APPLY 模式：单事务真实 upsert 写入 sn_assets；全量时同事务推进 checkpoint"
             "（未给时默认 DRY-RUN）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="只处理前 N 个非跳过行（按源流顺序）；APPLY 时跳过 checkpoint 推进",
    )
    parser.add_argument(
        "--allow-source-unavailable",
        action="store_true",
        help="SQL Server 连接受限时以 NOT RUN/exit 0 结束；不连接或写入 MySQL",
    )
    parser.add_argument(
        "--target-database",
        dest="target_database",
        default=None,
        metavar="NAME",
        help="覆盖目标库名（视为人工确认，可超出白名单；仍用 DATABASE_URL 同一套凭据连接）",
    )
    parser.add_argument(
        "--sn-column-map",
        dest="sn_column_map",
        default=None,
        metavar="JSON",
        help="覆盖 11 对列映射的 JSON（键必须与本地字段一致，仅允许改源列名）",
    )
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        print("[提示] 同时指定 --dry-run 与 --apply，以 --apply 为准（--dry-run 仅在未给 --apply 时生效）。")
    if args.limit is not None and args.limit < 1:
        print("[配置错误] --limit 必须为正整数，当前为 {!r}".format(args.limit))
        return 2

    # 列映射：默认镜像后端；允许 JSON 覆盖（只改源列名，键集合必须一致）
    if args.sn_column_map:
        try:
            override_map = json.loads(args.sn_column_map)
        except ValueError as e:
            print("[配置错误] --sn-column-map 不是合法 JSON: {}".format(e))
            return 2
        if not isinstance(override_map, dict):
            print("[配置错误] --sn-column-map 必须为 JSON 对象")
            return 2
        expected_keys = set(SN_LOCAL_FIELDS)
        got_keys = set(override_map.keys())
        if got_keys != expected_keys:
            print(
                "[配置错误] --sn-column-map 键集合与后端 11 对映射不一致；"
                "缺失: {0}；多余: {1}；允许键: {2}".format(
                    ", ".join(sorted(expected_keys - got_keys)) or "（无）",
                    ", ".join(sorted(got_keys - expected_keys)) or "（无）",
                    ", ".join(SN_LOCAL_FIELDS),
                )
            )
            return 2
        bad = [k for k, v in override_map.items() if not isinstance(v, str) or not v.strip()]
        if bad:
            print("[配置错误] --sn-column-map 存在空源列名: {}".format(", ".join(bad)))
            return 2
        column_map = {k: v.strip() for k, v in override_map.items()}
    else:
        column_map = dict(DEFAULT_SN_COLUMN_MAP)

    config = read_config()
    if config is None:
        return 2

    host = config["MSSQL_HOST"]
    port_raw = config["MSSQL_PORT"]
    user = config["MSSQL_USER"]
    password = config["MSSQL_PASSWORD"]
    database = config["MSSQL_DATABASE"]
    try:
        port = int(port_raw)
    except (ValueError, TypeError):
        print("[配置错误] MSSQL_PORT={!r} 不是有效整数".format(port_raw))
        return 2

    # 目标 MySQL：仓库根 .env（或进程环境）的 DATABASE_URL
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print(
            "[配置错误] 未找到 DATABASE_URL。"
            "请通过进程环境变量或仓库根目录 .env 提供（mysql+asyncmy://user:pass@host:port/dbname）。"
        )
        return 2
    try:
        db_info = _parse_mysql_url(db_url)
    except ValueError as e:
        print("[配置错误] 解析 DATABASE_URL 失败: {}".format(e))
        return 2
    if not db_info["database"]:
        print("[配置错误] DATABASE_URL 未包含库名（无法定位目标库）")
        return 2

    # 目标库白名单（镜像 DESTRUCTIVE_TEST_DATABASE_ALLOWLIST）
    if args.target_database is not None:
        target_db = args.target_database.strip()
        if not target_db:
            print("[配置错误] --target-database 不能为空")
            return 2
        # 显式指定 = 人工确认，允许任意库名，但仍用 DATABASE_URL 同一套凭据连接
    else:
        target_db = db_info["database"]
        if target_db not in ALLOWED_TARGET_DBS:
            print(
                "[配置错误] 目标库 {0!r} 不在允许名单，拒绝执行。允许的目标库名: {1}。\n"
                "如需连接其他库请显式传 --target-database（视为人工确认，"
                "凭据仍取自 DATABASE_URL），默认模式为 DRY-RUN 不写数据。".format(
                    target_db, ", ".join(ALLOWED_TARGET_DBS)
                )
            )
            return 2

    # 依赖检查：任一缺失即 exit 2（此时未连接数据库、未写任何数据）
    try:
        import pyodbc  # 惰性导入
    except ImportError:
        print("[错误] 未安装项目依赖 pyodbc，请先安装 requirements.txt")
        return 2
    try:
        import pymysql  # 惰性导入
    except ImportError:
        print("[错误] 未安装依赖 pymysql，请先执行: pip install pymysql")
        return 2

    # 运行标识：snoff-<epoch>-<pid>（写入 raw_data / checkpoint statistics）
    runid = "snoff-{0}-{1}".format(int(time.time()), os.getpid())

    # 模式文案
    if args.apply:
        if args.limit is not None:
            mode_str = "APPLY + --limit（子集试运行：写入但不推进 checkpoint）"
        else:
            mode_str = "APPLY（单事务真实写入 sn_assets，含 checkpoint 原子推进）"
    else:
        if args.limit is not None:
            mode_str = "DRY-RUN + --limit（只估算前 {0} 行，不写任何持久数据）".format(args.limit)
        else:
            mode_str = "DRY-RUN（默认：只校验 + 存在性估算，不写任何持久数据）"

    print("SN 离线批量同步（流式 upsert：insID 行身份校验） @ {}".format(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("源 SQL Server: {0}:{1} 库: {2} 用户: {3} 表: [{4}].[{5}]（只读 SELECT）".format(
        host, port, database, user, RELAY_SN_SCHEMA, RELAY_SN_TABLE))
    print("目标 MySQL: {0}:{1} 库: {2} 用户: {3} 表: [sn_assets]（行身份 source_system+ins_id）".format(
        db_info["host"], db_info["port"], target_db, db_info["user"]))
    print("模式: {0}".format(mode_str))
    if args.limit is not None:
        print("行数限制: 只处理前 {0} 个非跳过行（按源流顺序）".format(args.limit))

    mssql_conn = None
    mysql_conn = None
    try:
        try:
            mssql_conn = _connect_mssql(host, port, user, password, database)
        except Exception as e:
            detail = _mask_all(str(e), password, db_info["password"])
            if args.allow_source_unavailable:
                print("[NOT RUN] SQLSERVER_UNAVAILABLE: {}: {}".format(type(e).__name__, detail))
                print("OVERALL: NOT RUN (SQL Server 连接受限，未连接或写入 MySQL)")
                return 0
            print("[FAIL] 连接 SQL Server 失败: {}: {}".format(type(e).__name__, detail))
            return 1
        try:
            mysql_conn = _connect_mysql(db_info, target_db)
        except Exception as e:
            _close_safe(mssql_conn)
            print("[FAIL] 连接 MySQL 失败: {}: {}".format(
                type(e).__name__, _mask_all(str(e), password, db_info["password"])))
            return 1

        try:
            # 预检查 1：源表/映射源列存在（值参数化，不写源库）
            _check_source_table(mssql_conn, RELAY_SN_SCHEMA, RELAY_SN_TABLE, column_map)
            _check_source_ins_id_identity(
                mssql_conn,
                RELAY_SN_SCHEMA,
                RELAY_SN_TABLE,
                column_map["ins_id"],
            )
            # 预检查 2：目标 SAP 行身份唯一键必须已由 migration 建立
            if not _check_target_sn_unique_key(mysql_conn, target_db):
                raise RuntimeError("SN_ASSET_ROW_IDENTITY_INDEX_MISSING")

            # 一次性构建源 SELECT / 目标 upsert SQL / 行转换函数
            source_cols = [column_map[f] for f in SN_LOCAL_FIELDS]
            src_select_sql = _build_src_select_sql(source_cols)
            upsert_sql = _build_sn_assets_upsert_sql()
            row_to_write = _make_row_to_write(runid)

            # 通用核心：流式读取 -> 分批存在性预查 -> （apply 时）分批 upsert
            src_cur = mssql_conn.cursor()
            counts = stream_upsert_by_key(
                src_cur,
                mysql_conn,
                src_select_sql=src_select_sql,
                dst_table=SN_ASSETS_TABLE,
                dst_unique_col=SN_ASSETS_UNIQUE_COL,
                write_columns=SN_ASSETS_WRITE_COLUMNS,
                upsert_sql=upsert_sql,
                row_to_write=row_to_write,
                sql_expr_columns=SN_ASSETS_SQL_EXPR,
                update_skip_columns=SN_ASSETS_UPDATE_SKIP,
                dst_where_sql="`source_system`='sqlserver'",
                dry_run=not args.apply,
                max_processed=args.limit,
                progress_label=RELAY_SN_TABLE,
            )

            # checkpoint 与数据同事务（原子）：先写 checkpoint，再统一 COMMIT；
            # --limit 子集试运行不推进 checkpoint
            if args.apply and args.limit is None:
                counts["invalidated"] = _invalidate_missing_sqlserver_rows(mysql_conn, runid)
                _apply_checkpoint(mysql_conn, runid, counts)
            if args.apply:
                mysql_conn.commit()

            print("[读取] read_count={0} skipped_empty_sn={1} writable={2}".format(
                counts["read_count"], counts["skipped"], counts["processed"]))
            if not args.apply:
                print("[估算] estimated_inserted={0} estimated_updated={1}".format(
                    counts["inserted"], counts["updated"]))
                print("checkpoint: 未推进（DRY-RUN 不写任何持久数据）")
                print("OVERALL: PASS (DRY-RUN，未写任何持久数据)")
                return 0

            # ---- APPLY 路径（已提交）----
            print("[APPLY] inserted={0} updated={1}（存在性预查口径，与 upsert 实际效果一致）".format(
                counts["inserted"], counts["updated"]))
            if args.limit is None:
                print("checkpoint: 已推进（sqlserver_sn_assets，与数据同一事务提交）")
            else:
                print("checkpoint: 子集试运行，未推进 checkpoint")
            print("OVERALL: PASS")
            return 0
        except Exception as e:
            _rollback_safe(mysql_conn)
            print("[错误] {}".format(_mask_all(str(e), password, db_info["password"])))
            return 1
    finally:
        _rollback_safe(mysql_conn)
        _close_safe(mysql_conn)
        _close_safe(mssql_conn)


if __name__ == "__main__":
    sys.exit(main())
