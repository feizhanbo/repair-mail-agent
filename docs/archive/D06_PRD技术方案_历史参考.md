> ⚠️ 本文档为历史参考，不可作为开发依据。最新信息请查阅 docs/ 目录下的正式文档。
> 归档日期：2026-07-13
> 替代文档：docs/00-开发指导文档.md

---
# 邮件报修自动化系统 PRD 技术方案（数据库修订版）

## 当前实现基线（2026-07-05）

本 PRD 仍作为业务背景和目标范围参考。当前代码实现以 README、AI 开发进度文档和 `docs/alignment-audit/` 为准：

- 角色统一为 `admin/supervisor/operator`，旧 `viewer` 不再作为当前开发方向。
- `parse_results.apply_status/applied_by_user_id/applied_at` 已落地。
- 后端已完成接口级 RBAC，前端角色隐藏只作为交互优化。
- 前端已新增用户管理、个人信息、站内消息页面。
- 远程新库 `repair_system_codex_dev_test` 已完成迁移和端到端用户新增展示删除验证。
- 真实 IMAP/SMTP/OSS 尚未正式联调，自动发信仍默认关闭。


## 1. 项目定位与目标

本项目定位为"三个月内完成内部试运行系统"，核心目标是把客户邮件报修流程从人工识别、登记、追问、SN 校验，升级为可追溯的自动化闭环：

`邮件接收 -> 原文归档 -> 回复链识别 -> 模板/附件解析 -> AI 辅助判断 -> SN 校验 -> 自动追问/人工复核 -> 工单沉淀 -> 可导出`

已核对材料包括：项目计划 docx、两个 `.xls` 模板、现有数据库草案、5 封真实 `.eml` 测试邮件。真实邮件显示：报修信息可能在正文、历史回复链、Excel 附件、TXT 测试日志、RAR/PRC 附件或内嵌图片中，不能只按标准模板解析。

本期不接入 SAP，也不真实对接 IT 中转服务器；但数据库结构、状态流转和导出边界需要为后续集成预留空间。AI 只能作为辅助判断和字段建议，不作为唯一事实来源。

## 2. 总体架构

推荐沿用项目计划中的技术栈：后端 `Python + FastAPI`，前端 `React + Ant Design`，数据库 `MySQL 8.x`，任务调度 `APScheduler`，邮件 `IMAP/SMTP`，附件存储 `阿里云 OSS`。

后端按模块拆分：

- `mail_ingest`：IMAP 拉取、Message-ID 去重、原始 EML 保存、附件上传 OSS、邮件入库。
- `mail_threading`：基于 `Message-ID / In-Reply-To / References / 归一化主题 / 发件人域名 / SN 相似度` 归并邮件线程，支持人工合并/拆分。
- `parser`：正文清洗、标准 RMA 模板解析、Excel/TXT 附件解析、字段标准化、缺失字段识别。
- `ai_provider`：统一封装外部 API 和后续内网模型，提供分类、字段抽取、追问草稿、回复审查能力。
- `sn_registry`：SN/资产库和板卡寄北京规则的导入、校验、匹配。
- `ticket_workflow`：工单生成、状态机流转、乐观锁、状态日志、人工修改审计。
- `reply`：追问话术模板、草稿生成、人工审核、SMTP 发送、外发邮件归档、追问 3 次熔断。
- `manual_review`：人工复核任务、分配、优先级、推送通知、处理闭环。
- `observability`：系统日志、用户操作日志、后台任务日志、AI 文件日志索引。

目录结构：

```text
backend/
  app/api/v1/                 # REST API
  app/core/                   # 配置、日志、权限、数据库
  app/models/                 # SQLAlchemy ORM
  app/schemas/                # Pydantic DTO
  app/services/               # 业务服务：ticket/mail/parser/ai/reply
  app/repositories/           # 数据访问
  app/workers/                # APScheduler 任务与后台处理
  app/integrations/           # IMAP/SMTP/OSS/AI
  migrations/                 # Alembic 迁移
frontend/
  src/pages/                  # 控制台页面
  src/components/             # 可复用业务组件
  src/api/                    # API client
  src/stores/                 # 状态管理
infra/
  docker-compose.yml
  nginx/
  backup/
```

## 3. 核心业务流程与状态机

工单状态沿用计划文档给定状态：

`new_email -> parsed -> need_customer_info -> auto_replied -> manual_review -> ready_for_export -> closed / error`

关键规则：

- `new_email`：邮件归档入库后进入，原始 EML、正文、HTML、附件元数据必须已保存。
- `parsed`：规则解析或 AI 解析完成，但不代表字段可信，需要继续做完整性和 SN 校验。
- `need_customer_info`：缺少 SN、联系人、联系方式、公司、故障描述等关键字段。
- `auto_replied`：已生成或发送追问；一期默认只生成草稿，人工审核后发送。
- `manual_review`：SN 不确定、AI 低置信度、字段冲突、追问超限、系统异常均进入人工复核。
- `ready_for_export`：字段完整且 SN/客户/物料校验通过，可进入后续导出流程。
- `closed`：人工关闭、后续处理完成或无需处理。
- `error`：系统异常；修复后可回到 `parsed`，也可强制关闭。

回复链处理必须做到：客户对系统追问的回复不新建工单，而是合并到原 `email_thread` 和原 `repair_ticket`。系统外发追问时必须设置 `In-Reply-To` 和 `References`，并记录 SMTP 返回的 `Message-ID`。

## 4. 前端控制中台设计

控制台以内部运营效率为目标，不做营销式页面。推荐左侧导航 + 顶部状态筛选 + 详情抽屉/分屏工作台。

核心页面：

- 首页看板：今日新邮件、待解析、待人工、追问中、异常、可导出、AI 低置信度数量；展示最近任务失败、追问超限、待审核回复。
- 邮件中心：列表支持主题、发件人、时间、线程、解析状态、是否重复、是否有附件筛选；详情展示原文、清洗正文、HTML、邮件头、回复链时间线、OSS 附件下载/预览。
- 工单中心：展示工单状态、客户、联系人、明细数量、SN 校验结果、追问次数、负责人；详情页分为基础信息、明细、来源邮件、解析候选、SN 校验、状态日志。
- 人工复核队列：按 `low_confidence / sn_error / followup_limit / system_error / field_conflict / manual` 分类；支持领取、分配、确认字段、退回解析、生成追问、关闭、复查退回。
- 自动回复审核：展示缺失字段、追问轮次、话术版本、AI 生成草稿、收件人/抄送、发送风险提示；支持批准发送、驳回、人工改写。
- 基础资料管理：SN/资产库、板卡/物料寄北京规则、导入来源信息、异常行报告。
- AI 日志：按邮件/工单查看 AI 调用摘要、提示词版本、输入摘要、输出摘要、结构化关键结果、置信度和详细日志文件定位。
- 系统配置：OSS 配置、AI Provider、追问阈值、自动发送开关、角色权限、话术模板。

人工复核推送机制：

- 后端创建 `manual_review_tasks` 时同步写入 `notification_events`。
- 前端使用 `SSE` 或 `WebSocket` 接收待办推送；不可用时每 30-60 秒轮询兜底。
- 推送内容包含任务类型、优先级、工单号、触发原因、关联邮件。
- 一期优先站内通知；后续可扩展企业微信/邮件提醒。

## 5. 数据库规划

### 5.1 设计原则

一期数据库设计以"可闭环、可追溯、不过度前置"为原则：

- 邮件事实与工单事实分离：邮件是原始证据，工单是业务处理结果。
- 每封邮件独立存储，回复链通过线程关联，不把多封邮件正文拼成一个字段。
- 附件和原始 EML 不保存二进制，统一上传 OSS，数据库只保存对象元数据和引用关系。
- 规则/AI/附件解析结果单独保存为解析候选，工单表只保存当前采纳后的业务视图。
- AI 详细日志写项目 JSONL 日志文件，数据库只保存摘要索引和日志定位信息。
- 一期不建设 `mail_accounts`、`email_fetch_batches`、`import_batches`、`customers`、`customer_aliases`、`board_card_aliases`、`export_records`。
- 邮箱账号配置放 `.env` 或服务配置文件；拉取和导入过程统一由 `job_run_logs` 记录。
- 客户信息从邮件解析和 SN 库校验中获得，不单独建设客户主数据。
- SN 校验以 `sn_assets` 为核心，字段包括客户编号、客户名称、物料代码、物料名称、编号/SN。

### 5.2 一期表清单

一期完整数据库表共 **26 张**：

| 序号 | 分组 | 表名 | 说明 |
| --- | --- | --- | --- |
| 1 | 用户权限 | `users` | 控制台用户 |
| 2 | 用户权限 | `roles` | 角色 |
| 3 | 用户权限 | `user_roles` | 用户角色关系 |
| 4 | OSS 文件 | `oss_objects` | OSS 对象元数据 |
| 5 | 邮件归档 | `email_threads` | 邮件线程 |
| 6 | 邮件归档 | `emails` | 邮件存档 |
| 7 | 邮件归档 | `email_attachments` | 邮件附件 |
| 8 | 邮件归档 | `email_ticket_links` | 邮件工单关联 |
| 9 | 工单 | `repair_tickets` | 工单主表 |
| 10 | 工单 | `repair_ticket_items` | 工单明细 |
| 11 | 状态机 | `workflow_statuses` | 状态定义 |
| 12 | 状态机 | `workflow_transitions` | 状态流转规则 |
| 13 | 状态机 | `ticket_status_logs` | 工单状态日志 |
| 14 | 状态机 | `field_audit_logs` | 字段修改审计 |
| 15 | 解析校验 | `parse_results` | 解析结果 |
| 16 | 解析校验 | `sn_validation_results` | SN 校验结果 |
| 17 | 基础资料 | `sn_assets` | SN/资产校验库 |
| 18 | 基础资料 | `board_cards` | 板卡/物料寄北京规则 |
| 19 | 回复 | `reply_templates` | 回复模板 |
| 20 | 回复 | `reply_records` | 回复记录 |
| 21 | 人工复核 | `manual_review_tasks` | 人工复核任务 |
| 22 | 人工复核 | `notification_events` | 站内通知事件 |
| 23 | AI 日志 | `ai_call_logs` | AI 调用摘要索引 |
| 24 | 日志 | `operation_logs` | 用户操作日志 |
| 25 | 日志 | `system_event_logs` | 系统事件日志 |
| 26 | 日志 | `job_run_logs` | 后台任务运行日志 |

### 5.3 核心表设计摘要

#### `emails` 邮件存档表

每封真实邮件单独保存一行。关键字段包括：

- `message_id`：邮件头原始 `Message-ID`，用于证据保留、回复链对照和页面展示。
- `message_id_hash`：`Message-ID` 的 SHA256，用于唯一去重。
- `thread_id`：关联邮件线程。
- `raw_eml_oss_object_id`：原始 EML 在 OSS 中的对象 ID。
- `subject`：原始主题。
- `normalized_subject`：去除 `Re/Fwd/回复/转发` 后的归一化主题，用于线程归并。
- `text_body`, `html_body`, `clean_body`, `latest_reply_segment`：分别保存纯文本、HTML、清洗正文和最新回复段。
- `in_reply_to`, `references_header`：保存回复链头信息。
- `parse_status`, `intent_type`：记录解析状态和邮件意图。

#### `email_threads` 邮件线程表

用于归并同一个报修对话中的多封邮件。关键字段包括：

- `thread_key`：线程归并键。
- `normalized_subject`：线程归一化主题。
- `root_message_id`：根邮件 Message-ID。
- `latest_email_id`：线程中最新邮件。
- `ticket_id`：关联主工单。
- `email_count`：线程邮件数量。
- `merge_confidence`, `merge_reason`：自动归并置信度和依据。
- `manual_locked`：人工锁定后不允许自动拆分或重归并。

#### `email_attachments` 与 `oss_objects`

附件保存关系：

```text
emails 1 - N email_attachments
email_attachments N - 1 oss_objects
emails.raw_eml_oss_object_id N - 1 oss_objects
```

`email_attachments` 保存附件元数据：

- `email_id`：所属邮件。
- `oss_object_id`：附件文件对应 OSS 对象。
- `file_name`, `content_type`, `file_size`, `file_hash`：附件基础信息。
- `is_inline`, `content_id`：内嵌图片和 CID。
- `parse_status`, `extracted_text`, `extracted_json`, `parse_error`：附件解析状态和结果。

`oss_objects` 保存 OSS 文件对象元数据：

- `bucket`, `endpoint`, `object_key`, `object_version`
- `original_file_name`, `safe_file_name`
- `content_type`, `file_size`, `sha256_hash`, `etag`
- `source_type`: `raw_eml` / `email_attachment` / `manual_upload`
- `upload_status`, `error_message`

OSS key 为建议格式，实际生成规则可根据业务可追溯性、对象存储限制和实现可行性调整：

```text
repair-mail/{env}/{yyyy}/{mm}/{thread_id}/{email_id}/{sha256}-{safe_filename}
```

#### `repair_tickets` 工单主表

保存当前被采纳后的工单主信息。关键字段包括：

- `ticket_no`：工单编号。
- `current_status_code`：当前状态。
- `source_email_id`：首封来源邮件。
- `thread_id`：关联邮件线程。
- `customer_code`, `customer_name`：客户编号和客户名称，来自邮件解析或 SN 库反查。
- `contact_person`, `contact_phone`, `contact_email`：联系人信息。
- `request_date`, `mailing_address`, `problem_description`, `accessories`：报修信息。
- `missing_fields`, `conflict_fields`：缺失字段和冲突字段。
- `followup_count`, `max_followup_count`：追问次数和上限。
- `confidence_score`：当前采纳字段的整体置信度。
- `assigned_user_id`, `manual_locked`, `version`：人工处理、锁定和乐观锁。

#### `repair_ticket_items` 工单明细表

保存一张工单中的多块板卡或多个备件。关键字段包括：

- `ticket_id`, `line_no`
- `material_code`, `material_name`
- `sn`, `sn_asset_id`
- `quantity`
- `failure_description`, `failure_information`, `data_info`, `remarks`, `accessories`
- `validation_status`, `validation_message`
- `manual_locked`

#### `email_ticket_links` 邮件工单关联表

用于精确记录哪些邮件参与了某个工单：

- `source`：首封来源邮件。
- `reply`：客户补充邮件。
- `forward`：转发邮件。
- `outbound`：系统外发追问邮件。
- `manual`：人工关联邮件。

工单与邮件的关系分两层：

```text
repair_tickets.thread_id -> email_threads.id
email_ticket_links.email_id -> emails.id
email_ticket_links.ticket_id -> repair_tickets.id
```

#### `parse_results` 解析结果表

`parse_results` 是解析候选和证据链，不是"必须人工采纳后才能生成工单"。

同一封邮件可能产生多种解析结果：

- 规则解析正文。
- 规则解析 Excel/TXT 附件。
- AI 解析正文或历史回复链。
- 人工修正结果。

关键字段包括：

- `email_id`, `source_attachment_id`, `ticket_id`
- `parser_type`: `rule` / `ai` / `manual`
- `parser_version`, `intent_type`
- `extracted_fields`, `extracted_items`
- `missing_fields`, `conflict_fields`
- `confidence_score`, `field_confidences`, `evidence`
- `apply_status`: `pending` / `auto_applied` / `manually_applied` / `partially_applied` / `rejected`
- `applied_by_user_id`, `applied_at`

系统可在解析结果完整且置信度满足条件时自动应用解析结果并生成/更新工单；低置信度、字段冲突或 SN 不确定时进入人工复核。

#### `sn_assets` SN/资产校验库

一期核心基础资料表。字段包括：

- `customer_code`：客户编号。
- `customer_name`：客户名称。
- `material_code`：物料代码。
- `material_name`：物料名称。
- `sn`：编号/SN，唯一。
- `asset_status`: `valid` / `expired` / `disabled` / `unknown`
- `warranty_start_date`, `warranty_end_date`
- `source_file_name`, `source_file_hash`, `source_row_no`, `raw_data`
- `imported_by_user_id`, `imported_at`

校验时比较：

```text
repair_tickets.customer_code/name
repair_ticket_items.material_code/name
repair_ticket_items.sn
```

与：

```text
sn_assets.customer_code/name
sn_assets.material_code/name
sn_assets.sn
```

不一致时写入 `sn_validation_results`，并触发 `manual_review`。

#### `board_cards` 板卡/物料寄北京规则表

保存唯一物料代码对应的物料名称和寄北京规则：

- `material_code`
- `material_name`
- `need_ship_to_beijing`
- `shipping_address`
- `shipping_contact`
- `shipping_phone`
- `postal_code`
- `status`
- `source_file_name`, `source_file_hash`, `source_row_no`, `raw_data`
- `imported_by_user_id`, `imported_at`

#### `reply_records` 回复记录表

覆盖"生成草稿"和"实际发送"两个阶段：

- `ticket_id`, `related_email_id`, `outgoing_email_id`
- `template_id`, `reply_type`, `followup_round`
- `missing_fields`
- `to_addresses`, `cc_addresses`, `subject`
- `draft_body`, `final_body`
- `generate_source`, `ai_call_log_id`
- `review_status`, `reviewed_by_user_id`, `reviewed_at`
- `send_status`, `smtp_message_id`, `in_reply_to`, `references_header`, `sent_at`

#### `manual_review_tasks` 与 `notification_events`

`manual_review_tasks` 保存人工复核任务：

- `ticket_id`, `email_id`
- `task_type`, `priority`, `status`
- `description`, `trigger_reason`
- `assigned_user_id`, `claimed_by_user_id`, `resolved_by_user_id`
- `resolution`

`notification_events` 保存站内通知事件：

- `event_type`, `target_type`, `target_id`
- `title`, `content`, `priority`
- `recipient_user_id`, `recipient_role_code`
- `delivery_channel`, `delivery_status`, `read_at`
- `metadata`

#### 日志表

`ai_call_logs` 只保存 AI 调用摘要和日志文件定位信息：

- `trace_id`, `email_id`, `ticket_id`, `call_type`
- `provider_name`, `model_name`, `prompt_version`
- `input_summary`, `output_summary`, `parsed_key_result`
- `confidence_score`, `latency_ms`, `status`, `error_message`
- `log_file_path`, `log_line_no`, `log_record_hash`

完整 prompt、输入、输出和步骤细节写项目 JSONL 日志：

```text
logs/ai/{yyyy}/{mm}/{dd}/ai-{yyyyMMdd}.jsonl
```

`operation_logs` 记录用户操作，如字段修改、回复审核、线程合并。

`system_event_logs` 记录系统事件，如 IMAP/SMTP/OSS/DB 异常、服务启动、配置变更。

`job_run_logs` 记录后台任务运行，如 IMAP 拉取、邮件解析、回复发送、OSS 上传、基础数据导入、备份。

### 5.4 后续暂不进入一期的表

| 表 | 后续加入条件 |
| --- | --- |
| `mail_accounts` | 需要控制台维护多个邮箱账号时 |
| `email_fetch_batches` | `job_run_logs` 无法满足拉取审计时 |
| `import_batches` | 基础资料导入审计复杂到需要独立批次管理时 |
| `customers` | 需要独立客户主数据、联系人、SLA、权限时 |
| `customer_aliases` | 客户名称匹配需要别名体系时 |
| `board_card_aliases` | 物料唯一编号无法覆盖实际别名问题时 |
| `ai_execution_traces` | 需要在数据库中查询 AI 多步骤链路时 |
| `ai_execution_steps` | 同上 |
| `export_records` | 开始真实导出、SAP 或 IT 中转对接时 |

## 6. API 与接口设计

统一枚举清单以 `repair-mail-agent/docs/unified-enums.md` 为准，后端常量和前端标签必须同步该清单。

核心 DTO：

```text
TicketStatus = new_email | parsed | need_customer_info | auto_replied | manual_review | error | ready_for_export | closed
RoleCode = admin | supervisor | operator
IntentType = new_repair | customer_reply | internal_forward | irrelevant | unknown
ManualTaskType = low_confidence | sn_error | followup_limit | system_error | field_conflict | manual
ReplySendStatus = draft | queued | sent | failed | cancelled
ParseApplyStatus = pending | auto_applied | manually_applied | partially_applied | rejected
```

主要 API：

- `POST /api/v1/auth/login`：控制台登录。
- `GET /api/v1/emails`、`GET /api/v1/emails/{id}`：邮件列表和详情。
- `POST /api/v1/emails/{id}/reparse`：重新解析邮件。
- `POST /api/v1/email-threads/{id}/merge`、`POST /api/v1/email-threads/{id}/split`：人工合并/拆分线程。
- `GET /api/v1/tickets`、`GET /api/v1/tickets/{id}`：工单列表和详情。
- `PATCH /api/v1/tickets/{id}/fields`：人工修正字段，必须写 `field_audit_logs`。
- `POST /api/v1/tickets/{id}/transition`：状态流转，前端不得直接改状态字段。
- `GET /api/v1/manual-review/tasks`：人工队列。
- `POST /api/v1/manual-review/tasks/{id}/resolve`：完成复核任务。
- `POST /api/v1/replies/{ticket_id}/draft`：生成追问草稿。
- `POST /api/v1/replies/{reply_id}/approve-send`：审核并发送。
- `POST /api/v1/master-data/sn-assets/import`：导入 SN/资产校验库。
- `POST /api/v1/master-data/board-cards/import`：导入板卡/物料寄北京规则。
- `GET /api/v1/ai-logs`：查询 AI 调用摘要索引。
- `GET /api/v1/notifications/stream`：人工复核和异常通知推送。

## 7. 解析与 AI 策略

规则优先，AI 兜底：

- 标准 RMA 模板优先走确定性规则解析，字段包括：报修日期、联系人/电话、公司、返回地址、板卡型号、板卡编号/SN、故障描述、故障信息、数据、备注、附属物品。
- 多行板卡必须生成多条 `repair_ticket_items`。
- 对 `.xlsx/.xls/.txt/.pdf/.docx` 附件建立附件解析器；真实样本中 ACCO 邮件正文几乎为空，关键信息在 Excel 和 TXT 附件。
- 对免责声明、签名、历史回复链要做正文分段，优先解析最新回复段，再在历史链中补充缺失字段。
- AI 输出必须是结构化 JSON，包含意图、字段、缺失字段、字段置信度、证据片段、冲突字段。
- AI 详细 prompt、完整输入、完整输出和步骤细节写项目日志文件；数据库只保留 AI 调用摘要索引。
- 人工确认过的字段设置 `manual_locked` 或字段级锁，后续 AI 不能静默覆盖。

解析结果应用策略：

- 标准模板解析完整、字段置信度高、SN 校验通过时，系统自动将 `parse_results.apply_status` 置为 `auto_applied`，并生成/更新工单。
- 缺少关键字段但能识别同一客户或同一线程时，可生成待补充工单，进入 `need_customer_info`。
- 字段冲突、低置信度、SN 不匹配时，保留解析候选并进入 `manual_review`。
- 人工修正字段后写 `field_audit_logs`，并更新工单当前业务视图。

SN 校验规则：

- SN 是否存在。
- SN 是否有效/过期。
- SN 是否与客户编号/客户名称匹配。
- SN 对应物料代码/物料名称是否与邮件填写一致。
- 物料是否属于寄北京清单。
- 任一关键校验失败或置信度低进入 `manual_review`。

## 8. 测试计划

真实测试数据观察必须转成回归用例：

- `2块FOVI自检FAIL，1块QTMU校准FAIL.eml`：正文自由文本，多 SN，多板卡，RAR 附件。
- `8200板卡送修-20260625.eml`：正文表格、PRC 附件、内嵌图片 CID、大段免责声明。
- `ACCO STS8300 FOVIe叫修.eml`：正文几乎为空，Excel/TXT 附件承载主要报修信息。
- `华峰测试机板卡报修...eml`：多段历史回复链，同线程多次报修内容，需识别最新内容并避免误合并。
- `备件送修申请.eml`：自由文本多行备件送修，多设备多故障。

测试分层：

- 单元测试：邮件头解析、主题归一化、模板字段解析、SN 校验、状态机转换、OSS key 生成。
- 集成测试：IMAP 拉取模拟、附件上传 OSS mock、AI Provider mock、SMTP mock、工单生成。
- 回归测试：5 封真实邮件 + 标准模板 + 缺字段 + 错误 SN + 多次追问 + 重复邮件。
- 前端 E2E：人工复核、字段修改审计、回复审核发送、线程合并/拆分。
- 验收标准：标准邮件可自动生成工单；缺字段能生成追问；客户回复能合并同工单；SN 异常/低置信度/追问超限能转人工；控制台能追溯邮件原文、附件、AI 摘要、状态、人工修改。

## 9. 假设与默认决策

- 默认采用项目计划指定技术栈：FastAPI、React + Ant Design、MySQL、APScheduler。
- 一期自动回复默认"只生成草稿 + 人工审核"，真实自动发送通过功能开关逐步放开。
- 附件和原始 EML 统一上传阿里云 OSS，数据库只保存元数据和对象引用。
- SAP/IT 中转系统一期不真实对接，工单达到 `ready_for_export` 即满足后续导出预留。
- AI 前期使用外部 API，后续通过同一 `AI Provider` 接口切换内网模型。
- AI 详细日志写项目 JSONL 文件，数据库只保存摘要索引和日志定位。
- 真实邮件不保证符合模板，因此正文、附件、回复链和人工复核必须同等作为一期能力设计。
- 用户角色统一为 `admin` 系统管理员、`supervisor` 主管、`operator` 一般操作员。
- Redis、Prometheus、Node Exporter 不进入当前一期必需范围。
