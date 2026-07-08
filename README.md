# 邮件报修自动化系统

`repair-mail-agent` 是邮件报修自动化系统的一期内部试运行工程。当前代码基线已从工程骨架推进到“基础业务闭环 + DeepSeek AI 首轮接入 + RBAC 接口拦截 + 远程新库端到端验证”阶段。

## 当前实现基线

更新日期：2026-07-08

- 后端已实现认证、用户管理、邮件、工单、人工复核、解析结果、回复、基础资料、通知、AI 日志、系统信息、统计分析等 API。
- 前端已实现登录、首页看板、邮件中心、工单中心、人工复核、回复管理、统计分析、基础资料、用户管理、个人信息、通知中心、站内消息、AI 日志、系统配置等页面。
- 数据库 ORM 当前为 26 张业务表，额外由 Alembic 管理 `alembic_version`；最新迁移版本为 `9d2b7c4f1a30`。
- `parse_results` 已落地 `apply_status`、`applied_by_user_id`、`applied_at`，用于表达 `pending`、`auto_applied`、`manually_applied`、`partially_applied`、`rejected`。
- 角色体系统一为 `admin`、`supervisor`、`operator`，不再保留 `viewer` 方向。
- 后端已补接口级 RBAC：高危接口由后端拦截，前端角色隐藏只作为交互优化，不作为安全边界。
- DeepSeek 只在后端通过环境变量读取密钥，前端不接触 API key；当前邮件解析以“规则预解析 + LLM 必审”为准，AI 失败时保留规则候选并转人工复核。
- 自动发送默认关闭，配置文件默认 `reply_send_mode=human_review`、`AUTO_SEND_ENABLED=false`；系统会生成回复草稿，人工确认后进入发送流程；真实 IMAP、SMTP、OSS 仍未做正式联调。

远程新库验证记录：

- 验证库：`repair_system_codex_dev_test`。
- 已迁移到 `9d2b7c4f1a30`。
- 保留 1 个临时管理员账号：`codex_admin_validation`。
- 端到端验证用户已通过后端新增、前端展示、前端删除，并确认数据库中 `users` 与 `user_roles` 对应记录已删除。
- 远程 MySQL root 实际口令以容器环境和私有 `.env` 为准，不写入仓库文档。

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

当前限制：工单/邮件行级归属字段尚未完整建模，因此邮件和工单基础查看接口暂按登录态开放；人工任务当前按统一任务池展示，任务分配/转派仍由后端限制为 supervisor/admin。

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
- `IMAP_*`
- `SMTP_*`
- `OSS_*`
- `AI_PROVIDER`
- `AI_BASE_URL`
- `AI_MODEL`
- `AI_API_KEY`
- `AI_TIMEOUT_SECONDS`
- `AI_MAX_INPUT_CHARS`
- `AI_PROMPT_VERSION`
- `AUTO_SEND_ENABLED`
- `REPLY_SEND_MODE`
- `AUTO_SEND_MIN_CONFIDENCE`
- `MAX_FOLLOW_UP`
- `CONFIDENCE_THRESHOLD`

AI 默认方向：

```text
AI_PROVIDER=deepseek
AI_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-flash
```

如需更强模型，通过私有环境变量覆盖 `AI_MODEL=deepseek-v4-pro`。不要把 API key 写入源码、README、测试快照、日志样例或 Git 可追踪文件。

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

当前 Alembic 迁移：

- `0f2ae6ba263f_create_initial_schema.py`
- `9d2b7c4f1a30_add_parse_result_apply_status.py`

当前业务表数量为 26：

- 用户权限：`users`, `roles`, `user_roles`
- OSS 与邮件：`oss_objects`, `email_threads`, `emails`, `email_attachments`, `email_ticket_links`
- 工单：`repair_tickets`, `repair_ticket_items`
- 工作流与审计：`workflow_statuses`, `workflow_transitions`, `ticket_status_logs`, `field_audit_logs`
- 解析与校验：`parse_results`, `sn_validation_results`, `sn_assets`, `board_cards`
- 回复与人工复核：`reply_templates`, `reply_records`, `manual_review_tasks`, `notification_events`
- 日志：`ai_call_logs`, `operation_logs`, `system_event_logs`, `job_run_logs`

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

本次本地验证结果：

- 后端项目虚拟环境下 `python -m compileall app` 通过。
- 后端 `pytest` 通过，结果为 `45 passed`。
- 前端 Codex Node 24.14.0 环境下 `npm run typecheck` 通过。
- 前端 `npm run build` 通过，仅有 Vite 大 chunk 警告。

## 已知限制

- 真实 IMAP、SMTP、OSS 未正式联调。
- 当前测试配置默认人工确认模式，系统会生成回复草稿，人工确认后进入发送流程；生产自动发送需通过 `reply_send_mode=auto_send` 和 `AUTO_SEND_ENABLED=true` 等配置开启，并满足 AI 置信度、收件人和风险条件。
- 工单/邮件行级数据权限尚未建模，本轮只落接口级角色权限。
- 远程新库验证只记录结果；除用户确认外，不对业务库执行迁移、seed、db_smoke 或写入型验证。
- 前端仍需补浏览器级 E2E，覆盖登录、用户管理、工单详情、人工复核、回复审核。
