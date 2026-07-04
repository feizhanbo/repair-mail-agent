# 邮件报修自动化系统

## 项目简介

`repair-mail-agent` 是邮件报修自动化系统的一期内部试运行工程。系统面向客户邮件报修场景，目标是完成邮件归档、回复链识别、模板/附件解析、AI 辅助判断、SN 校验、追问草稿、人工复核和工单沉淀的闭环。

## 核心业务流程

```text
邮件接收 -> 原文归档 -> 回复链识别 -> 模板/附件解析 -> AI 辅助判断
-> SN 校验 -> 自动追问/人工复核 -> 工单沉淀 -> 可导出
```

## 技术栈

- 后端：Python 3.11、FastAPI、SQLAlchemy 2.x asyncio、Pydantic Settings、Alembic
- 数据库：MySQL 8.x、`utf8mb4`、`DATETIME(3)`
- 任务调度：APScheduler
- 文件存储：阿里云 OSS
- 邮件：IMAP 收信、SMTP 发信
- AI：统一 AI Provider 抽象层，当前默认 DeepSeek OpenAI 兼容接口
- 前端：React、TypeScript、Vite、Ant Design、TanStack Query、Zustand
- 部署：Docker Compose、Nginx、GitHub Actions

## 项目结构

```text
repair-mail-agent/
  backend/                 # FastAPI 后端服务
  frontend/                # React 控制台
  docker/                  # 后端/前端镜像构建文件
  nginx/                   # 统一入口配置
  docs/                    # 开发依据和阶段说明
  .github/workflows/       # CI/CD workflow
  docker-compose.yml       # MySQL、后端、前端、Nginx 编排
  deploy.sh                # 远程服务器部署脚本
```

## 当前开发进度

当前已推进到“基础业务开发 + DeepSeek AI 接入”阶段，重点完成后端业务 API、前端业务页面、AI 解析/追问草稿能力和本地只读验证。

- 后端已补齐认证依赖、统一响应、邮件、工单、人工复核、解析结果、回复、基础资料、通知、AI 日志和系统信息等 API。
- 工单侧支持字段编辑、明细编辑、SN 校验、状态流转、解析结果采纳、邮件时间线、附件、字段证据和状态日志查询。
- 人工复核侧支持任务队列、详情上下文、领取、释放、处理、重解析和解析结果采纳。
- AI 侧已接入 DeepSeek Provider 抽象，支持 JSON 输出解析、AI 解析候选、追问草稿生成、AI 调用日志摘要和失败回退；API key 只从运行环境读取。
- 前端已替换占位页，新增邮件中心、工单中心、人工复核、回复审核、基础资料、AI 日志和系统配置等页面。
- 当前不接真实 IMAP、SMTP、OSS，不自动发送邮件，`AUTO_SEND_ENABLED=false` 保持默认。

## 环境变量

配置示例见 `.env.example`。真实密钥不得提交到仓库，生产和试运行环境通过服务器 `.env` 或 GitHub Secrets 注入。

关键配置包括：`DATABASE_URL`、`REDIS_URL`、`JWT_SECRET`、`IMAP_*`、`SMTP_*`、`OSS_*`、`AI_*`、`AUTO_SEND_ENABLED`、`MAX_FOLLOW_UP`、`CONFIDENCE_THRESHOLD`。

当前数据库接入策略：

- 数据库账号暂用 `root`。
- 开发库名为 `repair_system_dev`。
- 真实 root 密码只保存到远程服务器 `.env`、本地私有 `.env` 和用户自己的密码管理器或离线安全记录。
- 本地开发通过 SSH 隧道连接远程 MySQL：`127.0.0.1:13307 -> remote 127.0.0.1:3307`。
- 本地 `DATABASE_URL` 示例：`mysql+asyncmy://root:<ROOT_PASSWORD>@127.0.0.1:13307/repair_system_dev`。
- 远程 Docker Compose 内 `DATABASE_URL` 示例：`mysql+asyncmy://root:<ROOT_PASSWORD>@mysql:3306/repair_system_dev`。
- 如果 root 密码包含特殊字符，写入 `DATABASE_URL` 时需要 URL 编码。
- 种子管理员密码通过私有 `.env` 的 `DEFAULT_ADMIN_PASSWORD` 注入，不写入 Git。
- MySQL 端口只绑定远程服务器 `127.0.0.1:3307`，不要开放公网数据库端口。

## 本地启动

当前阶段本地开发通过 SSH 隧道连接远程 Docker MySQL。数据库结构保持现有 ORM/Alembic 状态；除明确进行数据库维护外，不执行迁移、seed 或会写入数据的 smoke 脚本。

后端开发命令：

```bash
cd backend
python -m venv .venv
pip install -r requirements.txt
uvicorn app.main:app --reload
```

前端开发命令：

```bash
cd frontend
npm install
npm run dev
```

Docker 骨架启动命令：

```bash
cp .env.example .env
# 立即修改 .env 中的 MYSQL_ROOT_PASSWORD 和 DATABASE_URL，真实密码不要提交 Git
docker compose up -d mysql
```

远程 MySQL root 部署信息和验证清单见 `docs/remote-mysql-root-deployment.md`。

## 数据库迁移

已生成首个 Alembic 迁移并在远程 `repair_system_dev` 验证通过。

后续确需数据库结构变更时，先确认备份和迁移范围，再执行：

```bash
cd backend
alembic revision --autogenerate -m "describe schema change"
alembic upgrade head
python -m app.seed
python -m app.db_smoke
```

数据库实现差异记录：`oss_objects.object_key` 在数据库文档中为 `VARCHAR(700)`，但与 `bucket VARCHAR(128)` 组成 `utf8mb4` 联合唯一键时会超过 MySQL 3072 字节索引上限；当前实现调整为 `VARCHAR(640)`。

本次开发只读核验结果：

- 本地经 SSH 隧道连接远端 `repair_system_dev` 成功。
- 本地 ORM 26 张业务表与远端数据库业务表字段一致，差异数为 0；数据库额外表仅 `alembic_version`。
- 当前基础数据包含流程状态、流程流转、角色和回复模板；真实登录验收需要先通过合规方式初始化用户。

## 测试命令

当前前后端开发验证命令：

```bash
cd backend
python -m compileall app
pytest
```

```bash
cd frontend
npm run typecheck
npm run build
```

本次已验证：后端 `compileall` 通过，`pytest` 7 passed；前端 Node 20 下 `typecheck` 和 `build` 通过。Vite build 仅提示大 chunk 警告。

## Docker 部署

```bash
docker compose up -d --build
docker compose logs -f backend-api
docker compose logs -f nginx
```

## CI/CD

`.github/workflows/deploy.yml` 预留 main 分支自动部署流程。需要在 GitHub Secrets 中配置 `SERVER_HOST`、`SERVER_USER`、`SERVER_PORT`、`SERVER_SSH_KEY`、`SERVER_PROJECT_DIR`。

当前本地仓库使用 `main` 分支。创建 GitHub 私有仓库后，将本地仓库添加为 `origin` 并推送；远程服务器部署目录 `/root/bert/repair-mail-agent` 后续通过 `git pull` 或 CI/CD 更新非私有代码，远程 `.env` 保留在服务器本地。

## 日志与排障

- API logs：`docker compose logs -f backend-api`
- Nginx logs：`docker compose logs -f nginx`
- AI JSONL logs：`logs/ai/{yyyy}/{mm}/{dd}/ai-{yyyyMMdd}.jsonl`
- 后台任务日志：数据库 `job_run_logs`
- 系统事件日志：数据库 `system_event_logs`

## 常见问题

- 邮件未入库：检查 IMAP 配置、后台任务日志和 `system_event_logs`。
- OSS 上传失败：检查 `OSS_*` 配置、bucket 权限和对象 key。
- AI 调用失败：检查 `AI_PROVIDER`、`AI_BASE_URL`、`AI_API_KEY` 和 JSONL 日志。
- SMTP 发送失败：确认 `AUTO_SEND_ENABLED`、SMTP 凭据和邮件服务限制。
- 迁移失败：先确认数据库备份，再检查 Alembic revision 和 ORM metadata。
