# Tasks

- [x] Task 1: 切换默认数据库配置到测试库
  - [x] 1.1 修改 `backend/app/config.py`：`DB_NAME` 默认值 → `repair_system_test`，`DATABASE_URL` 默认值 → `repair_system_test`
  - [x] 1.2 修改 `.env.example`：`DB_NAME` 和 `DATABASE_URL` 默认值同步

- [x] Task 2: 创建一站式初始化脚本 `backend/db_bootstrap.py`
  - [x] 2.1 连接 MySQL（无具体数据库），检测 `repair_system_test` 是否存在
  - [x] 2.2 若不存在或有但无种子数据 → CREATE DATABASE → alembic upgrade head → python -m app.seed
  - [x] 2.3 若种子数据已存在 → 跳过建库/迁移/种子，打印提示
  - [x] 2.4 使用 openpyxl 读取 `D:\refile\SNdata.xlsx`，解析字段映射，调用 `import_sn_assets()` 写入
  - [x] 2.5 使用 xlrd 读取 `D:\refile\寄北京板卡.xls`（跳过标题行，第2行为表头），解析字段映射，调用 `import_board_cards()` 写入
  - [x] 2.6 每步输出进度和结果统计，数据文件不存在时跳过并警告

# Task Dependencies
- Task 2 依赖 Task 1（脚本需要读取 config.py 中的 DATABASE_URL）
