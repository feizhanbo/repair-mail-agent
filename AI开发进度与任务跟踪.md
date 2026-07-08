# AI开发进度与任务跟踪

更新日期：2026-07-08

## 1. 当前阶段

当前阶段为“内部试运行闭环硬化”。基础业务、DeepSeek AI 首轮接入、用户管理、个人信息、站内消息、`parse_results.apply_status`、人工处理流程优化、接口级 RBAC 已完成本地开发和验证。

本轮目标不是部署生产环境，而是把前后端、数据库结构和开发文档统一到同一条实现方向上。

## 2. 当前完成内容

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 后端 API | 已完成首轮并持续扩展 | 认证、用户、邮件、工单、人工复核、解析结果、回复、基础资料、通知、AI 日志、系统信息、统计分析均已接入 `/api/v1`。 |
| 后端 RBAC | 已完成首轮并按任务池调整 | `users` 仅 admin；人工任务列表/领取/处理允许 operator/supervisor/admin，分配仅 supervisor/admin；回复草稿和编辑允许 operator/supervisor/admin，审核通过/拒绝仅 supervisor/admin；基础资料导入仅 admin；AI 日志、统计分析和系统信息仅 supervisor/admin。 |
| 数据库 ORM | 已完成首轮 | 26 张业务表 + `alembic_version`；最新迁移为 `9d2b7c4f1a30`。 |
| `parse_results` | 已修复 | 已新增 `apply_status`、`applied_by_user_id`、`applied_at`，保留旧 `accepted/accepted_by_user_id/accepted_at` 兼容字段。 |
| 角色体系 | 已统一 | 仅保留 `admin`、`supervisor`、`operator`，不再沿用 `viewer`。 |
| 用户能力 | 已完成首轮 | 后端支持用户列表、新增、编辑、启停、角色分配、重置密码、物理删除；前端已实现用户管理页。 |
| 个人信息 | 已完成首轮 | 支持查看/编辑个人资料和修改当前用户密码。 |
| 站内消息 | 已完成首轮 | 支持消息列表、未读筛选、详情抽屉、标记已读。 |
| DeepSeek AI | 已完成首轮并改为解析必经环节 | Provider 抽象、JSON 输出校验、解析候选、置信度依据、人工处理方向、追问/回复草稿、AI 日志摘要、JSONL 定位和失败转人工。 |
| 系统配置 | 已完成首轮 | 运行期业务配置已通过 `backend/config/runtime_config.json` 持久化，支持 `reply_send_mode`、`auto_send_enabled`、`auto_send_min_confidence`、`confidence_threshold`、`max_follow_up`。 |
| 导入导出 | 已完成首轮 | 基础资料模板、导入、导出和工单导出已走 `.xlsx` 主流程，前端以 blob 下载。 |
| 统计分析 | 已完成首轮 | 已新增 `/statistics` 页面和 `/api/v1/statistics/summary`，支持周期/自定义日期与趋势、用户处理量、任务池等指标。 |
| 前端角色感知 | 已完成首轮 | 菜单和高危操作按钮按角色隐藏；后端 RBAC 作为最终安全边界。 |
| 远程新库验证 | 已完成 | `repair_system_codex_dev_test` 已迁移到 `9d2b7c4f1a30`，保留临时管理员 `codex_admin_validation`，验证用户已新增、展示、删除。 |

## 3. AI 接入设计

当前 AI Provider 仅在后端执行，不允许前端直连 DeepSeek。

默认配置：

```text
AI_PROVIDER=deepseek
AI_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-flash
AI_PROMPT_VERSION=deepseek-v4-json-v1
```

安全约束：

- `AI_API_KEY` 只从运行环境读取。
- 不把 API key 写入源码、文档、测试快照、构建产物或 Git 可追踪文件。
- AI 调用日志不记录 key。
- 完整 prompt/input/output 写入私有 JSONL；数据库只保存摘要、模型、耗时、状态、关键结构化结果和 JSONL 定位。
- AI 失败不阻断邮件入库或重解析；当前业务实现会保留规则候选并转入人工复核，不直接用规则结果自动建单或自动发送。

## 4. 业务闭环

当前闭环能力：

1. 手工邮件入库或既有邮件查询。
2. 规则预解析生成候选和上下文，DeepSeek AI 作为邮件业务判断必经环节生成最终解析候选。
3. 工单字段、明细、附件、邮件时间线、字段证据、状态日志展示。
4. 工单字段编辑、明细编辑、SN 校验。
5. 高置信且无冲突的 AI 解析结果自动应用；低置信、缺字段、字段冲突、AI 不可用等情况进入人工复核。
6. 人工复核任务进入统一任务池，支持全员可见、操作员领取、主管分配/转派、释放、处理、重解析。
7. 追问/确认回复草稿生成，AI 不可用时回退模板。
8. 回复草稿编辑；人工确认后发送，生产可通过配置切换满足条件自动发送。
9. 站内通知支持全局任务池通知、指定用户通知和角色通知。

## 5. 数据库状态

当前 ORM 和迁移覆盖 26 张业务表：

```text
users, roles, user_roles,
oss_objects, email_threads, emails, email_attachments, email_ticket_links,
repair_tickets, repair_ticket_items,
workflow_statuses, workflow_transitions, ticket_status_logs, field_audit_logs,
parse_results, sn_validation_results, sn_assets, board_cards,
reply_templates, reply_records, manual_review_tasks, notification_events,
ai_call_logs, operation_logs, system_event_logs, job_run_logs
```

`parse_results` 当前应用状态字段：

- `apply_status`
- `applied_by_user_id`
- `applied_at`
- 兼容字段：`accepted`、`accepted_by_user_id`、`accepted_at`

远程新库验证事实：

- 库名：`repair_system_codex_dev_test`
- 迁移版本：`9d2b7c4f1a30`
- 保留用户：临时管理员 `codex_admin_validation`
- 验证用户：已从前端删除，数据库中不再保留

真实 MySQL root 口令不写入文档，以容器环境和私有配置为准。

## 6. API 与权限

当前接口级 RBAC 方向：

- `users`：仅 `admin`。
- `manual-review`：列表、详情、领取、释放、处理、重解析允许 `operator/supervisor/admin`；列表默认按任务池全员可见；分配/转派仅 `supervisor/admin`。
- `replies`：草稿和编辑允许 `operator/supervisor/admin`；审核通过/拒绝仅 `supervisor/admin`。
- `master-data`：查询允许 `supervisor/admin`；导入仅 `admin`。
- `ai-logs`、`system`：允许 `supervisor/admin`。
- 邮件、工单基础查看和必要处理接口仍以登录态为主。
- 工单状态流转仅 `supervisor/admin`。
- operator 可查看统一人工任务池并领取未分配任务；主管分配/转派仍由后端 RBAC 限制。

行级工单/邮件隔离尚未实现，原因是当前模型缺少明确业务归属字段。本项进入后续设计。

## 7. 前端页面

当前页面清单：

- `/login`：登录。
- `/`：首页看板。
- `/emails`：邮件中心。
- `/tickets`：工单中心与详情工作台。
- `/manual-review`：人工复核工作台。
- `/replies`：回复审核。
- `/statistics`：统计分析，仅 supervisor/admin 展示入口。
- `/master-data`：SN 和板卡基础资料。
- `/users`：用户管理，仅 admin 展示入口。
- `/profile`：个人信息。
- `/notification-center`：通知中心，铃铛入口跳转页。
- `/notifications`：站内消息。
- `/ai-logs`：AI 日志，仅 supervisor/admin 展示入口。
- `/system`：系统配置，仅 supervisor/admin 展示入口。

## 8. 验证结果

本次本地验证：

- 后端 `python -m compileall app` 通过。
- 后端 `pytest` 通过，结果为 `45 passed`。
- 前端 `npm run typecheck` 通过。
- 前端 `npm run build` 通过，仅有 Vite 大 chunk 警告。

本次未执行：

- 新的数据库迁移执行。
- seed。
- db_smoke。
- 远程数据库写入。
- Git commit/push。
- 部署。

## 9. 当前限制与下一步

当前限制：

- 真实 IMAP、SMTP、OSS 未联调。
- 当前配置文件默认 `reply_send_mode=human_review` 且 `auto_send_enabled=false`，系统会生成回复草稿，人工确认后调用发送流程；生产可通过配置切换为满足条件自动发送。
- 工单/邮件行级数据权限未建模。
- 前端缺少完整 Playwright/E2E 回归。
- 真实 DeepSeek 调用仅能在私有环境变量配置后手动验收。

下一步建议：

1. 为核心 API 补更细的 `response_model` 和错误码目录。
2. 增加服务层业务闭环测试，覆盖邮件入库、解析、工单生成、复核、回复审核。
3. 设计行级数据权限字段和规则，不直接在现有模型上硬编码。
4. 补前端 E2E，覆盖 admin/supervisor/operator 三类用户。
5. 将 IMAP/SMTP/OSS 联调作为独立阶段执行。
