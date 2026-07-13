# 数据库 Schema 差异检查与测试数据库创建 Spec

## Why
当前项目代码层面通过 SQLAlchemy ORM（27个模型类，对应27张业务表）和3条 Alembic 迁移（latest: `b3e1f7d2a4c0`）定义了数据库结构，但从未与远程数据库 `repair_system_dev` 的实际 schema 做过系统性差异比对。此外，数据库文档（`02-数据库详细设计文档.md`）与代码实际状态存在多处不一致，需要以代码为事实基准进行核对。同时项目需要将当前代码的迁移执行到独立的测试数据库 `repair_system_test`，用于本地开发和自动化测试。

## 代码基线数据（当前事实）

### 27 张 ORM 表
```
users, roles, user_roles,
oss_objects,
email_threads, emails, email_attachments, email_ticket_links,
mail_fetch_records,
repair_tickets, repair_ticket_items,
workflow_statuses, workflow_transitions, ticket_status_logs, field_audit_logs,
parse_results, sn_validation_results,
sn_assets, board_cards,
reply_templates, reply_records,
manual_review_tasks, notification_events,
ai_call_logs, operation_logs, system_event_logs, job_run_logs
```

### 1 个数据库视图
- `business_emails` — 由迁移 `b3e1f7d2a4c0` 创建，无对应 ORM 模型

### Alembic 迁移链
```
(基线) → 0f2ae6ba263f (initial schema) → 9d2b7c4f1a30 (parse_results.apply_status) → b3e1f7d2a4c0 (emails重构 + mail_fetch_records + business_emails视图)
```

## What Changes
- 新增数据库 schema 差异检查脚本（`backend/db_diff.py`），连接到远程 `repair_system_dev`，提取实际 schema 并以 ORM 模型 + Alembic 迁移为基准逐项比对
- 输出差异报告：表/列/索引/约束/迁移版本/视图
- 新增测试数据库初始化脚本（`backend/db_setup.py`），创建 `repair_system_test` 并执行当前代码的 `alembic upgrade head` + seed
- 新增 `DB_NAME` / `TEST_DATABASE_URL` 环境变量配置支持多数据库切换
- 更新 `.env.example` 添加新配置项示例
- **不改动 `docker-compose.yml`**，不改动现有功能代码，不改动 ORM 模型

## Impact
- Affected specs: 无
- Affected code: `backend/app/config.py`（新增 `DB_NAME`、`TEST_DATABASE_URL`）、`backend/db_diff.py`（新增）、`backend/db_setup.py`（新增）、`.env.example`（新增配置示例）
- **BREAKING**: 无

## ADDED Requirements

### Requirement: Schema 差异检查工具
系统 SHALL 提供 `backend/db_diff.py`，连接远程 `repair_system_dev`，以 ORM 模型为基准输出结构化差异报告。

#### Scenario: 检查远程数据库表存在性
- **WHEN** 执行差异检查脚本
- **THEN** 以 ORM 定义的 27 张表为基准，输出"仅代码有"和"仅远程库有"的表列表

#### Scenario: 检查远程数据库字段差异
- **WHEN** 执行差异检查脚本
- **THEN** 对共有的表逐字段比对列名、类型、可空、默认值，输出不一致项（远程库实际值 vs ORM 定义值）

#### Scenario: 检查远程数据库索引差异
- **WHEN** 执行差异检查脚本
- **THEN** 对共有的表比对索引（名称、列、唯一性），输出差异

#### Scenario: 检查迁移版本同步状态
- **WHEN** 执行差异检查脚本
- **THEN** 比对远程 `alembic_version` 与本地 head `b3e1f7d2a4c0`，输出是否一致

#### Scenario: 检查数据库视图存在性
- **WHEN** 执行差异检查脚本
- **THEN** 检查远程数据库是否存在 `business_emails` 视图

#### Scenario: 远程数据库不可达时的处理
- **WHEN** 远程数据库连接失败
- **THEN** 输出明确的连接错误信息（网络/隧道/凭据），不抛出未捕获异常

### Requirement: 测试数据库创建与初始化
系统 SHALL 提供 `backend/db_setup.py`，在 MySQL 实例中创建 `repair_system_test` 并执行当前代码的 Alembic migration 和 seed。

#### Scenario: 创建测试数据库并执行迁移
- **WHEN** 执行 `python db_setup.py`
- **THEN** 创建 `repair_system_test`（utf8mb4），执行 `alembic upgrade head` 建表，执行 `python -m app.seed` 初始化种子数据

#### Scenario: 测试数据库与开发数据库隔离
- **WHEN** `repair_system_test` 和 `repair_system_dev` 同时存在
- **THEN** 表结构和种子数据一致，业务数据互相隔离

#### Scenario: 幂等初始化
- **WHEN** 目标数据库已存在
- **THEN** 跳过 CREATE DATABASE，仍执行 `alembic upgrade head` 和 seed

### Requirement: 多数据库配置支持
项目 SHALL 通过环境变量支持 dev/test 数据库切换，无需修改代码。

#### Scenario: 通过环境变量切换数据库
- **WHEN** 设置 `DATABASE_URL` 指向 `repair_system_test`
- **THEN** 后端服务连接并使用测试数据库
