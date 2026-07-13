# Tasks

- [x] Task 1: 创建 schema 差异检查脚本 `backend/db_diff.py`
  - [x] 1.1 连接远程 MySQL（使用配置中的连接信息），从 `information_schema` 提取实际表（BASE TABLE）的列、索引、外键信息
  - [x] 1.2 从 SQLAlchemy `Base.metadata` 提取 ORM 定义的 27 张表的列、索引、约束信息作为比对基准
  - [x] 1.3 实现比对逻辑：表存在性、列差异（名称/类型/可空/默认值/自增）、索引差异（名称/列/唯一性）、外键差异
  - [x] 1.4 比对 `alembic_version` 版本号与本地 head（`b3e1f7d2a4c0`）
  - [x] 1.5 查询 `information_schema.VIEWS` 检查 `business_emails` 视图是否存在
  - [x] 1.6 输出结构化差异报告（终端分组输出），差异项标注远程库实际值 vs ORM 定义值
  - [x] 1.7 添加连接失败的错误处理和友好提示

- [x] Task 2: 创建测试数据库初始化脚本 `backend/db_setup.py`
  - [x] 2.1 连接无具体数据库的 MySQL 实例，执行 `CREATE DATABASE IF NOT EXISTS repair_system_test DEFAULT CHARSET utf8mb4`
  - [x] 2.2 通过环境变量或参数切换 `DATABASE_URL` 指向新库，执行 `alembic upgrade head` 和 `python -m app.seed`
  - [x] 2.3 添加幂等性保证：数据库已存在时跳过创建，迁移和种子正常执行

- [x] Task 3: 新增多数据库配置支持
  - [x] 3.1 在 `backend/app/config.py` 的 `Settings` 中新增 `DB_NAME`（默认 `repair_system_dev`）和 `TEST_DATABASE_URL` 配置字段
  - [x] 3.2 更新 `.env.example` 添加 `DB_NAME` 和 `TEST_DATABASE_URL` 配置示例及注释

# Task Dependencies
- Task 2 依赖 Task 3（db_setup.py 需要配置切换机制）
- Task 1 独立执行，可与 Task 3 并行
