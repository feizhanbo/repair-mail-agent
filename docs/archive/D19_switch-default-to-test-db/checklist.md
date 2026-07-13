# Checklist

- [x] `config.py` 默认 `DATABASE_URL` 和 `DB_NAME` 已切换为 `repair_system_test`
- [x] `.env.example` 默认值已同步
- [x] `db_bootstrap.py` 能自动检测测试库种子数据状态（查询 workflow_statuses 记录数）
- [x] 未初始化时自动建库 + migration + seed
- [x] 已初始化时跳过建库/迁移/种子，直接导入主数据
- [x] SN 数据从 `D:\refile\SNdata.xlsx` 正确解析并 upsert 到 `sn_assets`
- [x] 板卡数据从 `D:\refile\寄北京板卡.xls` 正确解析（跳过标题行）并 upsert 到 `board_cards`
- [x] 数据库不可达时输出友好错误提示
- [x] 数据文件不存在时跳过并输出警告
- [x] 现有测试全部通过
