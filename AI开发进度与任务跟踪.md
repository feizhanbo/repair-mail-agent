# AI开发进度与任务跟踪

## 1. PRD 摘要

一期目标是完成邮件报修自动化内部试运行底座，覆盖邮件归档、回复链识别、结构化解析、SN 校验、追问草稿、人工复核、工单沉淀和可追溯控制台。默认关闭真实自动发送，附件和原始 EML 进入 OSS，数据库只保存元数据和对象引用。

## 2. 当前开发阶段

当前阶段：阶段 1 项目底座（骨架已搭建）。

本次任务已完成：在 `D:\code\repair-mail-agent` 全新搭建项目骨架，不复用 `D:\refile\backend`，实现一期 26 张表 ORM model。已按当前决策将数据库连接示例调整为 root 账号和 `repair_system_dev` 开发库，并补充远程 MySQL root 部署记录模板。远程 Docker MySQL 已部署在 `/root/bert/repair-mail-agent`，本地通过 SSH 隧道完成连接验证，首个 Alembic 迁移已生成并执行成功。基础种子数据已通过 ORM 初始化，`app.db_smoke` 已验证本地工程和远程服务器均可与远程 MySQL 交互。

## 3. 全量任务树

- 后端：FastAPI 工程、配置、统一响应、ORM model、Repository/Service/Worker 目录。
- 数据库：一期 26 张表 ORM、Alembic 初始迁移、基础种子数据和 DB smoke 检查已纳入本阶段。
- 前端：React + TypeScript + Ant Design 控制台骨架。
- 部署：Docker Compose、Nginx、GitHub Actions、部署脚本初版。
- 测试：静态编译、metadata 表数量检查；数据库集成测试后续补充。
- 文档：README、AI 进度文档、开发依据索引。

## 4. 模块完成度

| 模块 | 状态 | 备注 |
| --- | --- | --- |
| 项目骨架 | 已完成 | 根目录、后端、前端、部署骨架已创建 |
| 后端 API 底座 | 已完成 | 已提供健康检查和占位路由 |
| 数据库 ORM | 已完成 | 已按一期最终版 26 张表实现 |
| Alembic 迁移 | 已完成 | 已生成 `0f2ae6ba263f_create_initial_schema.py` 并在远程 `repair_system_dev` 执行成功 |
| 前端控制台 | 已完成 | 已完成 React + Ant Design 骨架 |
| Docker/Nginx/CI | 已完成 | 已落基础配置；MySQL Compose 当前使用 root 账号和 `repair_system_dev` |
| 种子数据 | 已完成 | 已通过 ORM 初始化状态、流转、角色、默认管理员和基础回复模板 |
| Git 本地仓库 | 已完成 | 已初始化本地 `main` 分支，`.env` 已确认被忽略；GitHub 私有远程仓库待创建 |

## 5. 数据库完成度

一期范围为 26 张表。本阶段已完成 ORM model、索引、唯一约束、外键定义和首个 Alembic 迁移；远程开发库已验证存在 26 张业务表。基础种子数据已初始化：8 个流程状态、16 条流程流转、4 个角色、1 个默认管理员、3 个基础回复模板。

当前数据库连接决策：开发阶段直接使用 root 账号，开发库为 `repair_system_dev`。真实 root 密码只允许保存在远程服务器 `.env`、本地私有 `.env` 和用户自己的密码管理器或离线安全记录，不进入仓库文档。

数据库实现差异：`oss_objects.object_key` 在数据库最终版文档中为 `VARCHAR(700)`，但与 `bucket VARCHAR(128)` 组成 `utf8mb4` 联合唯一键时超过 MySQL 3072 字节索引上限；当前 ORM 和迁移调整为 `VARCHAR(640)`。

## 6. API 完成度

当前只保留 `/health` 和 `/api/v1` 占位接口。认证、邮件、工单、人工复核、回复、基础资料、AI 日志、通知等业务逻辑未实现。

## 7. 前端页面完成度

当前只完成登录页、首页看板和核心模块占位页面。接口联调和业务操作待后续阶段。

## 8. 测试与验收进度

本阶段验收以目录结构、后端静态编译、ORM 表数量、数据库迁移、种子数据和前端源码结构为主。本地工程经 SSH 隧道连接远程 MySQL 的 smoke test 已通过；远程服务器经 `127.0.0.1:3307` 连接 MySQL 容器的 smoke test 已通过。接口集成和真实邮件回归待后续阶段。

## 9. 当前阻塞项

- 真实 IMAP/SMTP/OSS/AI 配置未提供。
- GitHub 私有远程仓库尚未创建；本机未安装 GitHub CLI，当前 GitHub 连接器也不提供创建仓库能力。
- 远程服务器部署目录已配置为 `/root/bert/repair-mail-agent`，当前通过同步包更新；待私有仓库创建后切换为 `git pull` 或 CI/CD 更新。

## 10. 下一步开发约束

- 每次开发前先读取本文件和 README。
- 不允许把真实密码、邮箱凭据、OSS key、AI key 写入仓库。
- 迁移前必须确认远程 Docker MySQL 和备份策略。
- 自动回复必须保持 `AUTO_SEND_ENABLED=false` 默认值。
