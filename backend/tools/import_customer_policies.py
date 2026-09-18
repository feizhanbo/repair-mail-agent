#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""《客户代码.xlsx》-> MySQL `customer_service_policies` 客户默认超保政策批量导入脚本。

用途
----
把客户清单 Excel 的每个客户导入为一条「默认超保」政策（``policy_type='default'``），
使 resolve_customer_policy() 能够按 ``customer_code`` 命中政策；同时对已存在其它
启用策略的存量客户不再新增政策行，只把其现有政策的 ``customer_scope`` 按 Excel
补齐（原先全库 37 条政策的 customer_scope 均为 NULL，策略解析返回
CUSTOMER_SCOPE_UNRESOLVED）。

模式
----
- DRY-RUN（默认，不加 ``--apply``）：只读取与统计，输出与 APPLY 完全相同的计数报告，
  ``ROLLBACK`` 收尾，不产生任何数据变更，退出码 0；
- APPLY（``--apply``）：以 ``policy_code`` 为唯一键 upsert（先批量查出已存在的
  policy_code 再决定 insert / update 字段），全部写入（含 scope 补齐）放在同一事务中
  一次性提交，保证重复执行幂等、不产生重复行。

目标库守卫（写入前必须通过，任一不符即退出码 2 且不建立写入连接）
---------------------------------------------------------------
``settings.DATABASE_URL`` 的 backend 必须为 mysql、host ∈ {127.0.0.1, localhost, ::1}、
port == 13307、库名 ∈ ``settings.DESTRUCTIVE_TEST_DATABASE_ALLOWLIST``。
连接驱动强制替换为 ``mysql+pymysql``（本机 asyncmy 连接该库偶发 1045，pymysql 正常），
并显式 charset=utf8mb4；所有数据值一律参数化绑定，绝不拼接进 SQL。

字段映射（详见 spec：.trae/specs/import-customer-service-policies/spec.md）
------------------------------------------------------------------------
- policy_code          = ``default-out-of-warranty-{业务伙伴代码大写}``（唯一键
  uk_customer_service_policies_code 已被 customer_code='*' 的全局默认行占用，
  故必须带客户代码后缀）
- customer_code        = 业务伙伴代码 strip().upper()
- customer_name        = 业务伙伴名称 strip()
- customer_scope       = 国内/保税区 -> domestic；境外/国外 -> overseas
- policy_type          = 'default'
- charge_status        = domestic -> 'chargeable'；overseas -> 'manual_confirmation'
- repair_price         = 1200.00
- currency             = domestic -> 'RMB'；overseas -> 'USD'
- tax_rate             = 13.0000
- shipping_fee_text    = 'one-way charge/单次收费'
- enabled              = 1
- effective_from/until = NULL
- source_file_name / source_row_no / imported_at = 溯源字段
- imported_by_user_id  = NULL（脚本无登录用户）
- 不显式写入 reply_salutation / hide_company_name / force_manual_review，保持库默认值

存量客户判定口径（Task 4）
--------------------------
查询库中所有 ``enabled=1`` 的政策并按 customer_code 归组；若某 Excel 客户已存在
**其它** policy_code 的启用策略（即 policy_code != 本脚本会生成的
``default-out-of-warranty-{CODE}``），则不新增行，只把该客户所有 ``enabled=1`` 且
``customer_scope IS NULL`` 的政策行按 Excel 映射补写 scope（已有值的绝不覆盖）。
本脚本自身生成的行 policy_code 恰为规则中的生成值，因此不会被判成存量客户，
重跑时走 update 分支，保证幂等。

已知偏离与风险（本脚本绕过服务层直接写库，以下偏离属已批准的 spec 记录项）
--------------------------------------------------------------------
1. 境内 default 政策直接收费；境外 default 政策按当前业务规则继续人工确认。
2. ``customer_code='*'`` 的全局默认行不在 Excel 中，本次不处理，
   其 ``customer_scope`` 仍为 NULL。
3. 不新增/修改 Alembic 迁移与表结构，不修改 API / 服务层 / 前端。

退出码：0=成功（dry-run 或 apply 均视为成功）/ 1=运行失败（解析、校验、读写等）/
2=配置或目标库守卫错误。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, create_engine, func, insert, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CustomerServicePolicy

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

# ---------------------------------------------------------------------------
# Excel 读取常量（按表头名定位，不依赖固定列序）
# ---------------------------------------------------------------------------
SHEET_NAME = "Sheet1"
COL_CUSTOMER_CODE = "业务伙伴代码"
COL_CUSTOMER_NAME = "业务伙伴名称"
COL_SCOPE_TEXT = "国内外"
REQUIRED_COLUMNS = (COL_CUSTOMER_CODE, COL_CUSTOMER_NAME, COL_SCOPE_TEXT)

# 国内外 -> customer_scope（国外与境外同义）
SCOPE_TEXT_TO_CUSTOMER_SCOPE = {
    "国内": "domestic",
    "保税区": "domestic",
    "境外": "overseas",
    "国外": "overseas",
}

# ---------------------------------------------------------------------------
# 政策常量字段
# ---------------------------------------------------------------------------
POLICY_CODE_PREFIX = "default-out-of-warranty"
POLICY_TYPE = "default"
CHARGE_STATUS_BY_SCOPE = {"domestic": "chargeable", "overseas": "manual_confirmation"}
REPAIR_PRICE = Decimal("1200.00")
TAX_RATE = Decimal("13.0000")
SHIPPING_FEE_TEXT = "one-way charge/单次收费"
CURRENCY_BY_SCOPE = {"domestic": "RMB", "overseas": "USD"}

# 写入批大小（单条 executemany 的行数）
WRITE_BATCH = 500


class ConfigError(RuntimeError):
    """配置 / 目标库守卫错误（退出码 2，且不建立写入连接）。"""


class ScriptError(RuntimeError):
    """运行期错误：解析校验失败、读写失败等（退出码 1）。"""


# ---------------------------------------------------------------------------
# 目标库守卫
# ---------------------------------------------------------------------------
def _validate_target() -> None:
    """校验 DATABASE_URL 指向本地 SSH 隧道上的白名单测试库，否则抛 ConfigError。

    该检查在建立任何数据库连接之前执行（守卫失败时不建立写入连接）。
    """
    url = make_url(settings.DATABASE_URL)
    allowed_databases = {
        name.strip() for name in settings.DESTRUCTIVE_TEST_DATABASE_ALLOWLIST if name.strip()
    }
    try:
        port = int(url.port or 3306)
    except (TypeError, ValueError) as exc:
        raise ConfigError("IMPORT_TARGET_MUST_MATCH_DATABASE_URL_ON_LOCAL_TUNNEL") from exc
    if (
        url.get_backend_name() != "mysql"
        or (url.host or "") not in {"127.0.0.1", "localhost", "::1"}
        or port != 13307
        or not url.database
        or url.database not in allowed_databases
    ):
        raise ConfigError("IMPORT_TARGET_MUST_MATCH_DATABASE_URL_ON_LOCAL_TUNNEL")


def _build_engine():
    """建立同步 SQLAlchemy 引擎（强制 mysql+pymysql 驱动 + utf8mb4）。"""
    url = make_url(settings.DATABASE_URL).set(drivername="mysql+pymysql")
    query = dict(url.query)
    query.setdefault("charset", "utf8mb4")
    url = url.set(query=query)
    return create_engine(url, pool_pre_ping=True)


def _mask(text: str) -> str:
    """掩码连接串里的密码，绝不把密码打印到终端。"""
    try:
        password = make_url(settings.DATABASE_URL).password
    except Exception:  # pragma: no cover - URL 解析失败时不掩码
        password = None
    if password and password in text:
        return text.replace(password, "***")
    return text


# ---------------------------------------------------------------------------
# Task 1：读取与校验
# ---------------------------------------------------------------------------
def _cell_text(value: Any) -> str:
    """单元格 -> 去空白字符串（None -> ''；整数值浮点去掉小数尾巴）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _read_excel(source_path: Path) -> dict[str, Any]:
    """读取 Sheet1 并按表头名定位三列，返回解析结果与统计。

    校验规则（任一不满足即抛 ScriptError，不写库）：
    - 业务伙伴代码 strip 后非空、按大写归一后全局唯一（同时保证 policy_code 批内不重复）
    - 业务伙伴名称 strip 后非空
    - 国内外 ∈ {国内, 境外, 保税区, 国外}

    返回：records（每行含 row_no / customer_code / customer_name / scope_text /
    customer_scope / policy_code）、scope_counter、blank_rows。
    """
    import openpyxl  # 惰性导入：未安装时仅本步骤报错

    workbook = openpyxl.load_workbook(source_path, data_only=True, read_only=True)
    if SHEET_NAME not in workbook.sheetnames:
        raise ScriptError(
            "EXCEL_SHEET_MISSING:{0}（实际工作表：{1}）".format(
                SHEET_NAME, ", ".join(workbook.sheetnames)
            )
        )
    sheet = workbook[SHEET_NAME]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        raise ScriptError("EXCEL_HEADER_MISSING:{0}".format(SHEET_NAME))

    column_index: dict[str, int] = {}
    for position, cell in enumerate(header):
        title = _cell_text(cell)
        if title and title not in column_index:
            column_index[title] = position
    missing = [name for name in REQUIRED_COLUMNS if name not in column_index]
    if missing:
        raise ScriptError(
            "EXCEL_COLUMN_MISSING:{0}（实际表头：{1}）".format(
                ", ".join(missing),
                ", ".join(_cell_text(cell) for cell in header),
            )
        )

    records: list[dict[str, Any]] = []
    problems: list[str] = []
    blank_rows = 0
    scope_counter: Counter[str] = Counter()
    seen_code: dict[str, int] = {}
    seen_policy_code: dict[str, int] = {}

    for row_no, row in enumerate(rows, start=2):  # 第 1 行为表头，数据从第 2 行开始
        def raw(column: str) -> Any:
            position = column_index[column]
            return row[position] if position < len(row) else None

        raw_code = raw(COL_CUSTOMER_CODE)
        raw_name = raw(COL_CUSTOMER_NAME)
        raw_scope = raw(COL_SCOPE_TEXT)
        code = _cell_text(raw_code)
        name = _cell_text(raw_name)
        scope_text = _cell_text(raw_scope)

        if not code and not name and not scope_text:
            blank_rows += 1  # 整行空白（Excel 导出常见）不计为数据行
            continue
        if not code:
            problems.append(
                "第 {0} 行：{1} 为空（原始值={2!r}）".format(row_no, COL_CUSTOMER_CODE, raw_code)
            )
            continue
        if not name:
            problems.append(
                "第 {0} 行：{1} 为空（原始值={2!r}，代码={3}）".format(
                    row_no, COL_CUSTOMER_NAME, raw_name, code
                )
            )
            continue
        if scope_text not in SCOPE_TEXT_TO_CUSTOMER_SCOPE:
            problems.append(
                "第 {0} 行：{1} 取值越界（原始值={2!r}，代码={3}；允许值={4}）".format(
                    row_no,
                    COL_SCOPE_TEXT,
                    raw_scope,
                    code,
                    "/".join(SCOPE_TEXT_TO_CUSTOMER_SCOPE),
                )
            )
            continue

        code_upper = code.upper()
        if code_upper in seen_code:
            problems.append(
                "第 {0} 行：{1} 重复（代码={2}，首次出现在第 {3} 行）".format(
                    row_no, COL_CUSTOMER_CODE, code, seen_code[code_upper]
                )
            )
            continue
        policy_code = "{0}-{1}".format(POLICY_CODE_PREFIX, code_upper)
        if policy_code in seen_policy_code:
            problems.append(
                "第 {0} 行：policy_code 重复（{1}，首次出现在第 {2} 行）".format(
                    row_no, policy_code, seen_policy_code[policy_code]
                )
            )
            continue
        seen_code[code_upper] = row_no
        seen_policy_code[policy_code] = row_no

        scope_counter[scope_text] += 1
        records.append(
            {
                "row_no": row_no,
                "customer_code": code_upper,
                "customer_name": name,
                "scope_text": scope_text,
                "customer_scope": SCOPE_TEXT_TO_CUSTOMER_SCOPE[scope_text],
                "policy_code": policy_code,
            }
        )

    if problems:
        raise ScriptError(
            "EXCEL_VALIDATION_FAILED（共 {0} 处问题）：\n  - {1}".format(
                len(problems), "\n  - ".join(problems)
            )
        )
    return {
        "records": records,
        "scope_counter": scope_counter,
        "blank_rows": blank_rows,
    }


# ---------------------------------------------------------------------------
# Task 2：字段映射与政策行构造
# ---------------------------------------------------------------------------
def _build_policy_row(
    record: dict[str, Any],
    *,
    source_file_name: str,
    imported_at: datetime,
) -> dict[str, Any]:
    """按 spec 字段映射构造一条政策行的写入参数（不显式写入模型默认列）。"""
    scope = record["customer_scope"]
    return {
        "policy_code": record["policy_code"],
        "customer_code": record["customer_code"],
        "customer_name": record["customer_name"],
        "policy_type": POLICY_TYPE,
        "charge_status": CHARGE_STATUS_BY_SCOPE[scope],
        "customer_scope": scope,
        "effective_from": None,
        "effective_until": None,
        "repair_price": REPAIR_PRICE,
        "currency": CURRENCY_BY_SCOPE[scope],
        "tax_rate": TAX_RATE,
        "shipping_fee_text": SHIPPING_FEE_TEXT,
        "enabled": True,
        "source_file_name": source_file_name,
        "source_row_no": record["row_no"],
        "imported_by_user_id": None,
        "imported_at": imported_at,
    }


# ---------------------------------------------------------------------------
# Task 3/4：存量判定、写入计划与 upsert
# ---------------------------------------------------------------------------
def _load_policies(session: Session) -> list[Any]:
    """读取全部政策行的关键列，一次取回后在内存里判定。"""
    table = CustomerServicePolicy.__table__
    return list(
        session.execute(
            select(
                table.c.id,
                table.c.policy_code,
                table.c.customer_code,
                table.c.customer_scope,
                table.c.enabled,
            )
        ).all()
    )


def _build_plan(records: list[dict[str, Any]], existing_rows: list[Any]) -> dict[str, Any]:
    """构造写入计划：待插入 / 待更新 / 跳过（存量客户）/ scope 补齐。

    - 存量客户：库中 ``enabled=1`` 的政策里存在其它 policy_code（!= 本脚本生成的
      default-out-of-warranty-{CODE}）的客户，不新增行；
    - scope 补齐：存量客户所有 ``enabled=1`` 且 ``customer_scope IS NULL`` 的行；
    - 其余客户按 policy_code 是否已存在分为 待插入 / 待更新。
    """
    existing_by_code = {str(row.policy_code): row for row in existing_rows}
    enabled_by_customer: dict[str, list[Any]] = defaultdict(list)
    for row in existing_rows:
        if int(row.enabled or 0) == 1:
            enabled_by_customer[str(row.customer_code or "").strip().upper()].append(row)

    insert_records: list[dict[str, Any]] = []
    update_records: list[dict[str, Any]] = []
    skipped_customers: list[str] = []
    disabled_only_customers: list[str] = []
    scope_backfill: list[dict[str, Any]] = []  # {id, customer_scope, customer_code}
    customers_with_any_policy = {
        str(row.customer_code or "").strip().upper() for row in existing_rows
    }

    for record in records:
        code = record["customer_code"]
        generated_code = record["policy_code"]
        current = enabled_by_customer.get(code, [])
        has_other_enabled_policy = any(
            str(row.policy_code) != generated_code for row in current
        )
        if has_other_enabled_policy:
            skipped_customers.append(code)
            for row in current:
                if row.customer_scope is None:
                    scope_backfill.append(
                        {
                            "id": int(row.id),
                            "customer_scope": record["customer_scope"],
                            "customer_code": code,
                        }
                    )
            continue
        if not current and code in customers_with_any_policy:
            # 该客户只有 enabled=0 的历史政策：按 enabled=1 口径不属存量客户
            disabled_only_customers.append(code)
        if generated_code in existing_by_code:
            update_records.append(record)
        else:
            insert_records.append(record)

    # 最终（预期）scope 分布：仅统计 Excel 客户
    projected: Counter[str] = Counter()
    skipped_set = set(skipped_customers)
    backfill_by_id = {item["id"]: item["customer_scope"] for item in scope_backfill}
    for record in records:
        code = record["customer_code"]
        if code not in skipped_set:
            projected[record["customer_scope"]] += 1
            continue
        scopes = set()
        for row in enabled_by_customer.get(code, []):
            scope = row.customer_scope
            if scope is None:
                scope = backfill_by_id.get(int(row.id))
            scopes.add(scope)
        if len(scopes) == 1:
            projected[scopes.pop()] += 1
        else:  # 理论不可达：同一客户启用策略的 scope 不一致
            projected["<conflict>"] += 1

    return {
        "insert_records": insert_records,
        "update_records": update_records,
        "skipped_customers": sorted(skipped_customers),
        "disabled_only_customers": sorted(disabled_only_customers),
        "scope_backfill": scope_backfill,
        "projected_scope_distribution": projected,
    }


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    """按 size 切批（避免单条 SQL 参数过多）。"""
    return [items[index : index + size] for index in range(0, len(items), size)]


def _update_statement():
    """按 policy_code 定位的 upsert-update 语句（所有值均以 bindparam 参数绑定）。"""
    table = CustomerServicePolicy.__table__
    return (
        update(table)
        .where(table.c.policy_code == bindparam("b_policy_code"))
        .values(
            customer_name=bindparam("b_customer_name"),
            policy_type=bindparam("b_policy_type"),
            charge_status=bindparam("b_charge_status"),
            customer_scope=bindparam("b_customer_scope"),
            effective_from=bindparam("b_effective_from"),
            effective_until=bindparam("b_effective_until"),
            repair_price=bindparam("b_repair_price"),
            currency=bindparam("b_currency"),
            tax_rate=bindparam("b_tax_rate"),
            shipping_fee_text=bindparam("b_shipping_fee_text"),
            enabled=bindparam("b_enabled"),
            source_file_name=bindparam("b_source_file_name"),
            source_row_no=bindparam("b_source_row_no"),
            imported_by_user_id=bindparam("b_imported_by_user_id"),
            imported_at=bindparam("b_imported_at"),
            updated_at=bindparam("b_updated_at"),
        )
    )


def _scope_backfill_statement():
    """scope 补齐语句：只改 customer_scope / updated_at，且仅在当前为 NULL 时生效。"""
    table = CustomerServicePolicy.__table__
    return (
        update(table)
        .where(table.c.id == bindparam("b_id"), table.c.customer_scope.is_(None))
        .values(
            customer_scope=bindparam("b_customer_scope"),
            updated_at=bindparam("b_updated_at"),
        )
    )


def _apply_writes(
    session: Session,
    plan: dict[str, Any],
    *,
    source_file_name: str,
    imported_at: datetime,
) -> dict[str, int]:
    """单事务执行写入：先 insert 新行，再 update 已有生成行，最后补 scope。"""
    table = CustomerServicePolicy.__table__
    written = {"inserted": 0, "updated": 0, "scope_backfilled": 0}

    insert_rows = [
        _build_policy_row(record, source_file_name=source_file_name, imported_at=imported_at)
        for record in plan["insert_records"]
    ]
    for batch in _chunks(insert_rows, WRITE_BATCH):
        session.execute(insert(table), batch)
        written["inserted"] += len(batch)

    if plan["update_records"]:
        updates = []
        for record in plan["update_records"]:
            row = _build_policy_row(
                record, source_file_name=source_file_name, imported_at=imported_at
            )
            updates.append(
                {
                    "b_policy_code": row["policy_code"],
                    "b_customer_name": row["customer_name"],
                    "b_policy_type": row["policy_type"],
                    "b_charge_status": row["charge_status"],
                    "b_customer_scope": row["customer_scope"],
                    "b_effective_from": row["effective_from"],
                    "b_effective_until": row["effective_until"],
                    "b_repair_price": row["repair_price"],
                    "b_currency": row["currency"],
                    "b_tax_rate": row["tax_rate"],
                    "b_shipping_fee_text": row["shipping_fee_text"],
                    "b_enabled": row["enabled"],
                    "b_source_file_name": row["source_file_name"],
                    "b_source_row_no": row["source_row_no"],
                    "b_imported_by_user_id": row["imported_by_user_id"],
                    "b_imported_at": row["imported_at"],
                    "b_updated_at": imported_at,
                }
            )
        statement = _update_statement()
        for batch in _chunks(updates, WRITE_BATCH):
            session.execute(statement, batch)
            written["updated"] += len(batch)

    if plan["scope_backfill"]:
        params = [
            {
                "b_id": item["id"],
                "b_customer_scope": item["customer_scope"],
                "b_updated_at": imported_at,
            }
            for item in plan["scope_backfill"]
        ]
        statement = _scope_backfill_statement()
        for batch in _chunks(params, WRITE_BATCH):
            result = session.execute(statement, batch)
            written["scope_backfilled"] += int(result.rowcount or 0)

    return written


def _verify_after_apply(session: Session, records: list[dict[str, Any]]) -> dict[str, Any]:
    """写入后复核：总行数、Excel 客户的 scope 分布、policy_code 是否重复。"""
    table = CustomerServicePolicy.__table__
    total = int(
        session.execute(select(func.count()).select_from(table)).scalar() or 0
    )
    excel_codes = [record["customer_code"] for record in records]
    rows = list(
        session.execute(
            select(table.c.customer_code, table.c.customer_scope, table.c.policy_code).where(
                table.c.enabled == 1,
                table.c.customer_code.in_(excel_codes),
            )
        ).all()
    )
    scope_by_customer: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        scope_by_customer[str(row.customer_code).strip().upper()].add(row.customer_scope)
    distribution: Counter[str] = Counter()
    conflicts = 0
    unresolved: list[str] = []
    for record in records:
        code = record["customer_code"]
        scopes = scope_by_customer.get(code, set())
        if not scopes:
            unresolved.append(code)
            continue
        if len(scopes) > 1:
            conflicts += 1
        scope = next(iter(scopes))
        if scope is None:
            unresolved.append(code)
        else:
            distribution[scope] += 1
    duplicate_codes = [
        code
        for code, count in Counter(
            str(row.policy_code) for row in session.execute(select(table.c.policy_code)).all()
        ).items()
        if count > 1
    ]
    return {
        "total_rows": total,
        "scope_distribution": distribution,
        "customers_with_multiple_enabled_policies": conflicts,
        "customers_without_scope": sorted(unresolved),
        "duplicate_policy_codes": duplicate_codes,
        "enabled_rows_for_excel_customers": len(rows),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _run(args: argparse.Namespace) -> int:
    source_path = Path(args.file).expanduser()
    if not source_path.is_file():
        raise ScriptError("SOURCE_FILE_NOT_FOUND:{0}".format(source_path))
    source_file_name = source_path.name

    print("== 客户默认超保政策导入（{0}）==".format("APPLY" if args.apply else "DRY-RUN"))
    print("输入文件: {0}".format(source_path))
    print("目标库  : {0}（本地隧道 {1}:{2}）".format(
        settings.database_name,
        make_url(settings.DATABASE_URL).host,
        make_url(settings.DATABASE_URL).port,
    ))
    print(
        "业务规则: 1) 境内 default 使用 RMB 并直接收费；"
        "2) 境外 default 使用 USD 并继续人工确认；"
        "3) customer_code='*' 的全局默认行不在 Excel 中，本次不处理。"
    )

    parsed = _read_excel(source_path)
    records: list[dict[str, Any]] = parsed["records"]
    scope_counter: Counter[str] = parsed["scope_counter"]
    print("[解析] 工作表 {0}：数据行={1} 整行空白行={2}".format(
        SHEET_NAME, len(records), parsed["blank_rows"]))
    print("[解析] 国内外分布：{0}".format(
        " ".join("{0}={1}".format(key, scope_counter[key]) for key in SCOPE_TEXT_TO_CUSTOMER_SCOPE)
    ))

    imported_at = datetime.now()
    engine = _build_engine()
    try:
        with Session(engine) as session:
            existing_rows = _load_policies(session)
            plan = _build_plan(records, existing_rows)
            skipped = plan["skipped_customers"]
            projected: Counter[str] = plan["projected_scope_distribution"]
            print("[存量] 跳过（已存在其它启用策略的客户）={0}：{1}".format(
                len(skipped), ", ".join(skipped) if skipped else "（无）"))
            disabled_only = plan["disabled_only_customers"]
            if disabled_only:
                print(
                    "[提示] 另有 {0} 个客户在库中只有 enabled=0 的历史政策，按 enabled=1 口径"
                    "不算存量客户，本次会为其新增默认政策行：{1}".format(
                        len(disabled_only), ", ".join(disabled_only)
                    )
                )
            print("[计数] 待插入={0} 待更新={1} 跳过={2} scope补齐={3}".format(
                len(plan["insert_records"]),
                len(plan["update_records"]),
                len(skipped),
                len(plan["scope_backfill"]),
            ))
            print("[分布] 最终 customer_scope 分布（仅 Excel 客户，预期 domestic=1048 overseas=240）：{0}".format(
                " ".join("{0}={1}".format(key, projected[key]) for key in sorted(projected))
            ))
            print(
                "[口径] 跳过判定 = 该客户在库中已有 enabled=1 且 policy_code 不等于 "
                "default-out-of-warranty-<客户代码> 的策略；scope 补齐 = 这些客户 "
                "enabled=1 且 customer_scope IS NULL 的行（已有值不覆盖）。"
            )

            if not args.apply:
                session.rollback()
                print("[DRY-RUN] 未写入任何数据（事务已回滚）。OVERALL: PASS")
                return EXIT_OK

            written = _apply_writes(
                session,
                plan,
                source_file_name=source_file_name,
                imported_at=imported_at,
            )
            session.commit()
            verified = _verify_after_apply(session, records)
            print("[APPLY] inserted={0} updated={1} scope_backfilled={2}".format(
                written["inserted"], written["updated"], written["scope_backfilled"]))
            print("[复核] customer_service_policies 总行数={0}".format(verified["total_rows"]))
            print("[复核] Excel 客户 scope 分布（实际）：{0} 未解析客户={1} 多策略客户={2}".format(
                " ".join(
                    "{0}={1}".format(key, verified["scope_distribution"][key])
                    for key in sorted(verified["scope_distribution"])
                ),
                len(verified["customers_without_scope"]),
                verified["customers_with_multiple_enabled_policies"],
            ))
            if verified["customers_without_scope"]:
                print("[复核][警告] 以下客户仍无 customer_scope：{0}".format(
                    ", ".join(verified["customers_without_scope"])))
            if verified["duplicate_policy_codes"]:
                raise ScriptError(
                    "POLICY_CODE_DUPLICATED:{0}".format(
                        ", ".join(verified["duplicate_policy_codes"]))
                )
            print("[复核] policy_code 无重复。OVERALL: PASS")
            return EXIT_OK
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "把客户清单 Excel 的客户导入 customer_service_policies 为默认超保政策"
            "（默认 dry-run，只统计不写库；--apply 才真实写入）"
        )
    )
    parser.add_argument(
        "--file",
        required=True,
        help="客户清单 Excel 绝对路径（读取 Sheet1，按表头名定位业务伙伴代码/业务伙伴名称/国内外）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真实写入数据库；缺省为 dry-run（只输出计数报告，不产生任何数据变更）",
    )
    args = parser.parse_args(argv)

    try:
        _validate_target()
    except ConfigError as exc:
        print("[配置错误] {0}".format(exc))
        print(
            "要求 DATABASE_URL 为 mysql、host ∈ {{127.0.0.1, localhost, ::1}}、port=13307、"
            "库名 ∈ DESTRUCTIVE_TEST_DATABASE_ALLOWLIST={0}。未建立任何写入连接。".format(
                settings.DESTRUCTIVE_TEST_DATABASE_ALLOWLIST
            )
        )
        return EXIT_CONFIG
    except Exception as exc:  # DATABASE_URL 无法解析等
        print("[配置错误] {0}: {1}".format(type(exc).__name__, _mask(str(exc))))
        return EXIT_CONFIG

    try:
        return _run(args)
    except ScriptError as exc:
        print("[失败] {0}".format(_mask(str(exc))))
        return EXIT_FAILED
    except Exception as exc:  # 连接/读写等运行期错误
        print("[失败] {0}: {1}".format(type(exc).__name__, _mask(str(exc))))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
