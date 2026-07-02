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
- AI：统一 AI Provider 抽象层
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

当前阶段已完成工程骨架、ORM model 和首个 Alembic 迁移。本地通过 SSH 隧道连接远程 Docker MySQL 后再执行迁移和联调。

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

后续模型变更后执行：

```bash
cd backend
alembic revision --autogenerate -m "describe schema change"
alembic upgrade head
python -m app.seed
python -m app.db_smoke
```

数据库实现差异记录：`oss_objects.object_key` 在数据库文档中为 `VARCHAR(700)`，但与 `bucket VARCHAR(128)` 组成 `utf8mb4` 联合唯一键时会超过 MySQL 3072 字节索引上限；当前实现调整为 `VARCHAR(640)`。

## 测试命令

```bash
cd backend
python -m compileall app
python -m app.db_smoke
pytest
```

```bash
cd frontend
npm run typecheck
npm run build
```

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
