# 初始化测试数据库并导入主数据 Spec

## Why
项目需要一份一键执行的脚本：自动检测测试数据库 `repair_system_test` 是否已初始化（含种子数据），并根据检测结果决定初始化策略；同时将 `D:\refile` 下的 SN 资产数据（`SNdata.xlsx`）和板卡寄北京规则（`寄北京板卡.xls`）导入数据库对应的 `sn_assets` 和 `board_cards` 表。

## 数据文件结构（已现场检查）

### SNdata.xlsx
- 路径：`D:\refile\SNdata.xlsx`
- 25 行数据，1 个 Sheet
- 列映射：`客户`→customer_code, `客户名称`→customer_name, `物料代码`→material_code, `物料名称`→material_name, `SN`→sn
- 其他字段默认：`asset_status="valid"`, `warranty_start_date=None`, `warranty_end_date=None`

### 寄北京板卡.xls
- 路径：`D:\refile\寄北京板卡.xls`
- 旧格式 .xls，需 xlrd 读取
- 第 1 行为标题行（"返修板卡寄送地址"），第 2 行为表头，数据从第 3 行起
- 列映射：`板卡型号`→material_code, `板卡名称`→material_name, `收货地址`→shipping_address
- 其他字段默认：`need_ship_to_beijing=True`, `shipping_contact=None`, `shipping_phone=None`, `postal_code=None`, `status="active"`

### 包年免费.xls / 报修格式附件.xls
- 与当前导入需求无关，不处理

## What Changes
- 新增 `backend/db_bootstrap.py`：一站式数据库初始化 + 主数据导入脚本
  - 自动检测测试库种子数据是否已存在
  - 未初始化时：创建库 → alembic upgrade head → seed
  - 已初始化时：跳过建库和种子
  - 始终执行：导入 SN 资产数据 + 导入板卡规则数据（upsert 逻辑）
- 修改 `backend/app/config.py` 默认 `DATABASE_URL` 和 `DB_NAME` 指向 `repair_system_test`
- 更新 `.env.example` 默认值同步
- **不改动 `docker-compose.yml`**，不改动业务代码，不改动 ORM 模型

## Impact
- Affected specs: `db-schema-audit-test-db`（延续）
- Affected code: `backend/app/config.py`（修改默认值）、`backend/db_bootstrap.py`（新增）、`.env.example`（修改默认值）
- **BREAKING**: 默认数据库从 dev 切换到 test

## ADDED Requirements

### Requirement: 默认数据库切换为测试数据库
`config.py` 的默认 `DATABASE_URL` 和 `DB_NAME` 改为指向 `repair_system_test`，`.env.example` 同步。

#### Scenario: 未设置环境变量时连接测试库
- **WHEN** 不设置 `DATABASE_URL` 直接启动
- **THEN** 连接 `repair_system_test`

### Requirement: 一站式数据库初始化与主数据导入脚本
系统 SHALL 提供 `backend/db_bootstrap.py`，实现自动检测 + 按需初始化 + 主数据导入。

#### Scenario: 测试库不存在或种子数据未导入
- **WHEN** 执行 `python db_bootstrap.py`，检测到 `workflow_statuses` 表无记录
- **THEN** 创建 `repair_system_test` 数据库 → 执行 `alembic upgrade head` → 执行 `python -m app.seed` → 导入 SN 资产数据 → 导入板卡规则数据 → 输出每一步的结果

#### Scenario: 测试库已存在且种子数据已导入
- **WHEN** 执行 `python db_bootstrap.py`，检测到 `workflow_statuses` 表有记录
- **THEN** 跳过建库/迁移/种子，直接导入 SN 资产数据和板卡规则数据，输出跳过原因

#### Scenario: SN 资产数据导入
- **WHEN** 读取 `D:\refile\SNdata.xlsx`
- **THEN** 解析 25 条 SN 记录，按 `sn` 字段 upsert 写入 `sn_assets` 表，输出 created/updated 数量

#### Scenario: 板卡规则数据导入
- **WHEN** 读取 `D:\refile\寄北京板卡.xls`
- **THEN** 解析板卡记录（跳过第 1 行标题，第 2 行为表头），按 `material_code` 字段 upsert 写入 `board_cards` 表，输出 created/updated 数量

#### Scenario: 数据库不可达
- **WHEN** 数据库连接失败
- **THEN** 输出友好错误提示，不抛出未捕获异常

#### Scenario: 数据文件不存在
- **WHEN** `D:\refile\SNdata.xlsx` 或 `寄北京板卡.xls` 不存在
- **THEN** 跳过该文件导入，输出警告信息，继续执行其他步骤
