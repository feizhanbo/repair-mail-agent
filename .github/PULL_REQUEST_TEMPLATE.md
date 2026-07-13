## 变更说明

<!-- 简要描述本次PR的变更内容 -->

## 文档同步检查

<!-- 请确认以下检查项（在 [ ] 中填 x 表示已完成） -->

- [ ] docs/01-开发进度文档.md 已更新变更记录
- [ ] 数据库变更（新增表/字段/索引/迁移）已同步 docs/02-数据库详细设计文档.md
- [ ] API变更（新增/修改/删除接口）已同步 docs/03-详细开发细节文档.md
- [ ] 页面交互变更已同步 docs/03-详细开发细节文档.md
- [ ] 配置变更（环境变量/Docker/Nginx/CI/CD）已同步 docs/05-研发与部署规范.md
- [ ] 安全相关变更（SMTP白名单/权限/密钥）已同步 docs/00-开发指导文档.md
- [ ] 业务流程/AI Prompt/模板变更已同步 docs/04-项目开发资料文档.md

## 影响范围

<!-- 勾选本次变更影响的模块 -->

- [ ] 后端 API
- [ ] 前端页面
- [ ] 数据库
- [ ] 部署配置
- [ ] AI/Agent
- [ ] 文档

## 验证

<!-- 描述如何验证本次变更 -->

- [ ] 后端：`python -m compileall app && pytest`
- [ ] 前端：`npm run typecheck && npm run build`
- [ ] 数据库：`alembic upgrade head && python -m app.seed && python -m app.db_smoke`
