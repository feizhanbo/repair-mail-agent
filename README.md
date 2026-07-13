# 邮件报修自动化系统

> 📖 详细开发文档请查阅 docs/ 目录。开发前必读：docs/00-开发指导文档.md 和 docs/01-开发进度文档.md。

`repair-mail-agent` 是邮件报修自动化系统的一期内部试运行工程，已实现基础业务闭环 + DeepSeek AI 接入 + RBAC 接口拦截。

## 核心业务流程

```text
邮件接收/手工入库
-> 原文归档与回复链识别
-> 规则预解析生成候选和 LLM 上下文
-> DeepSeek AI 判断邮件类型、字段有效性、异常情况和置信度
-> 高置信无冲突时自动应用到工单；否则进入人工复核
-> 工单生成/字段修正/明细修正
-> SN 校验
-> 解析候选采纳或拒绝
-> 人工复核任务领取/分配/处理
-> 追问草稿生成
-> 主管审核回复
-> 工单状态流转与审计沉淀
```

## 技术栈

- 后端：Python 3.11、FastAPI、SQLAlchemy 2.x asyncio、Pydantic Settings、Alembic。
- 数据库：MySQL 8.x、`utf8mb4`、`DATETIME(3)`。
- AI：DeepSeek OpenAI 兼容接口，后端 Provider 抽象，JSON 输出校验。
- 前端：React、TypeScript、Vite、Ant Design、TanStack Query、Zustand。
- 部署预留：Docker Compose、Nginx、GitHub Actions。

## 角色权限

| 角色 | role_code | 当前后端拦截 |
| --- | --- | --- |
| 系统管理员 | `admin` | 用户管理、角色分配、系统配置、基础资料导入、全部业务兜底操作。 |
| 主管 | `supervisor` | 查看业务数据、人工任务分配/转派/释放、回复审核、AI 日志、系统配置、工单状态流转。 |
| 一般操作员 | `operator` | 查看统一人工任务池，主动领取未分配任务，处理本人已领取/被分配任务，修正字段、SN 校验、采纳解析、生成追问、提交回复草稿。 |

行级数据权限尚未完整建模，当前按角色 + 接口级 RBAC 控制；详细限制见 docs/01。

## 项目结构

```text
repair-mail-agent/
  backend/                 # FastAPI 后端服务
  frontend/                # React 控制台
  docker/                  # 镜像构建文件
  nginx/                   # 统一入口配置
  docs/                    # 开发、审计和数据库对照文档
  .github/workflows/       # CI/CD workflow 预留
  docker-compose.yml       # MySQL、后端、前端、Nginx 编排
  deploy.sh                # 远程部署脚本
```

## 环境变量

配置示例见 `.env.example`。真实密钥、数据库口令、邮箱凭据、OSS key、AI key 不得提交到仓库。

关键配置：

- `DATABASE_URL`
- `JWT_SECRET`
- `IMAP_*`、`SMTP_*`、`OSS_*`
- `AI_PROVIDER`（默认 `deepseek`）
- `AI_BASE_URL`（默认 `https://api.deepseek.com`）
- `AI_MODEL`（默认 `deepseek-v4-flash`，可选覆盖为 `deepseek-v4-pro`）
- `AI_API_KEY`
- `AI_TIMEOUT_SECONDS`
- `AI_MAX_INPUT_CHARS`
- `AI_PROMPT_VERSION`
- `AUTO_SEND_ENABLED`
- `REPLY_SEND_MODE`
- `AUTO_SEND_MIN_CONFIDENCE`
- `MAX_FOLLOW_UP`
- `CONFIDENCE_THRESHOLD`

## 本地启动

后端：

```bash
cd backend
python -m venv .venv
pip install -r requirements.txt
uvicorn app.main:app --reload
```

前端：

```bash
cd frontend
npm install
npm run dev
```

数据库维护命令只在明确确认后执行。默认开发和检查不运行迁移、seed、db_smoke 或任何会写入真实业务库的命令。

## 数据库与迁移

当前 Alembic 迁移版本：`9d2b7c4f1a30`，共 27 张业务表 + alembic_version（用户权限、OSS、邮件、工单、工作流审计、解析校验、回复复核、日志等），详见 docs/ 目录下的数据库对照文档。

## 验证命令

后端：

```bash
cd backend
python -m compileall app
pytest
```

前端：

```bash
cd frontend
npm run typecheck
npm run build
```

## 已知限制

详见 docs/01-开发进度文档.md 中的已知限制章节。
