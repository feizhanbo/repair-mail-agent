# Checklist

- [x] `backend/db_diff.py` 可正常执行，以 27 张 ORM 表为基准输出表/列/索引/外键/迁移版本/视图对比报告
- [x] 差异报告清晰展示差异项，含具体的字段名/索引名、远程库实际值、ORM 定义值
- [x] 远程数据库不可达时输出友好错误提示而非未捕获异常
- [x] `backend/db_setup.py` 可创建 `repair_system_test` 并完成 `alembic upgrade head` + seed
- [x] `db_setup.py` 对已存在的数据库支持幂等运行
- [x] `backend/app/config.py` 新增 `DB_NAME` / `TEST_DATABASE_URL` 配置项且向后兼容
- [x] `.env.example` 已更新包含新配置项示例和注释
