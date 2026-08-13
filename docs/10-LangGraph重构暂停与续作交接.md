# LangGraph 重构暂停与续作交接

> 暂停时间：2026-08-13  
> 当前目标状态：主动暂停，目标仍未完成；不是技术阻塞。  
> 工作区：`D:\test_graph\repair-mail-agent`  
> Git 断点：分支 `codex-dev`，基线提交 `34625bf`；本轮改动均在未提交工作树中。  
> 重要提醒：当前改动尚未提交，工作树包含本轮全部重构内容，不得 reset、checkout 或覆盖。

## 1. 下次恢复时先读

按以下顺序恢复上下文：

1. `docs/06-当前代码业务流程与状态体系分析.md`：代码审计、真实流程、状态机、20 个必答问题。
2. `docs/07-LangChain与LangGraph重构方案.md`：目标架构、State、Node/Router、HITL、幂等、迁移和回滚。
3. `docs/08-重构实施进度与回归记录.md`：各阶段已实施内容和发布前事项。
4. `docs/09-LangGraph重构验收证据矩阵.md`：需求—代码—测试—外部签收矩阵。
5. 本文：准确暂停点和下一步顺序。

## 2. 已完成的代码范围

- 完成现状深度审计、真实业务流程与工单状态体系还原。
- 完成 LangChain 统一 AI Gateway：DeepSeek、Qwen、Qwen-VL、structured output、模型配置、异常和 usage/trace。
- 完成附件能力标准化：类型检测、Normalized Schema、确定性解析与条件式语义理解。
- 完成 ZIP/RAR/7Z/TAR/GZIP 安全检查、受限递归解包和成员 Parser Router。
- 完成 DOCX 段落、表格、内嵌图片计数/提取；仅含图片时使用 Qwen-VL。
- 完成 Shadow Graph 和权威 Active Graph；`langgraph` 模式不再先运行 legacy 大编排。
- 完成 Graph State、Node、Conditional Router、统一可恢复错误边界。
- 已补齐全部 Router 的直接表驱动分支测试，以及主要 Node 的直接输入/输出、Service 调用和 fail-closed 测试；不再仅依赖 Graph 端到端间接覆盖。
- 修复 SAP `pending` 路由缺失：确认远端不存在后最多允许一次安全重提，并以 `sap_submit_attempt_count` 有界控制；连续 pending 进入人工。
- SAP 重提次数通过 `sap_submit_export_id` 限定在单个 export；人工 reparse 产生新 export 时自动重置。
- `poll_sap` 内部 reconcile 已由 Graph adapter 显式关闭 legacy Job/RMA Job 调度；真实 `pending` 结果通过 Conditional Edge 最多安全重提一次，达到上限统一转人工，避免 Graph 与旧 worker 双提交。
- 生产 `active_graph.py` 已只保留唯一权威 Active Graph；旧 validation/SAP 迁移切片仅作为测试夹具存在。
- 新执行标记为 `langgraph-v2`，State schema 维持 v1 兼容；execution ID 与 workflow/thread/email 身份绑定不可变，冲突时拒绝复用旧 checkpoint。
- 完成 MySQL workflow execution/interrupt 索引和 PostgreSQL checkpointer 分工。
- 修正 workflow 审计外键删除语义：业务邮件、工单、Job、人工任务或用户被现有删除流程移除时仅置空审计关联，不阻断删除；execution 删除仍级联 interrupt。
- schema 发布审计现已核对两张 workflow 表的列、唯一约束、外键名称及删除动作；离线 Alembic SQL 测试也检查对应 DDL。
- opt-in MySQL 删除集成测试已加入 execution/interrupt 保留与置空断言，但本机 `127.0.0.1:3307` 无数据库，尚未执行该真实 FK 场景。
- PostgreSQL Compose 服务已设为 `langgraph` profile；部署脚本只在 `WORKFLOW_ENGINE=langgraph` 时启动并初始化它，legacy 部署只要求 MySQL。
- Graph 发布审计会在应用启动前检查业务 MySQL revision 与 workflow checkpoint identity 列，migration 不完整时拒绝切换；legacy 不执行该门禁。
- 完成 LangGraph `interrupt()` / `Command(resume=...)` 人工审核恢复。
- 修复 Resume 跨存储崩溃窗口：interrupt ledger 保存 PostgreSQL checkpoint ID；若上次 Resume 已推进 checkpoint 但 MySQL 提交失败，Job 重试只对账账本，不会把旧人工 payload 再注入下一 interrupt。
- checkpoint 对账进一步加入单调 step：缺失、无法证明顺序或 step 回退都 fail-closed，不会把空/旧 checkpoint 误记为已完成。
- 修复 `resume_queued` 卡死：确定性终止失败会释放 interrupt 租约并重新开放人工 task；人工再次提交复活同一幂等 Job。外部 scheduler interrupt 由 admin retry API 显式恢复，不能绕过人工审核。worker 租约过期时不会立即开放第二次 Resume，而是等待管理员确认旧 worker 已停止。
- 完成 SAP submit/reconcile/poll、RMA prepare/send/archive、普通回复 prepare/send 的副作用拆分和幂等恢复。
- 完成稳定灰度配置：email allowlist + SHA-256 百分比分桶。
- 完成 Graph Node 级 started/result/duration/error/route delta 可观测性；AI token、Job retry、外部 operation、human interrupt 复用现有日志/台账。
- runtime-status 已增加 Graph waiting/failed 和 pending interrupt error 计数。
- 保留 `legacy` 默认引擎作为迁移期回滚入口，未提前删除旧代码。

## 3. 当前验证基线

当前最新工作树在 `backend` 目录的验证结果：

```text
$env:PYTHONPATH='.'; python -m pytest -q
703 passed, 7 skipped, 12 warnings

$env:PYTHONPATH='.'; python -m pytest -q tests/test_job_leases.py tests/test_workflow_config.py tests/test_p0_consistency.py tests/test_graph_resume_job_recovery.py tests/test_api_contract.py
88 passed, 2 warnings

git diff --check
passed（只有 Git 的 LF→CRLF 提示）

python -m alembic heads
r5m0h1c2d3e4 (head)
```

Job 租约、HITL 所有权、邮件级单活派发和 Graph 部署/应用启动证据门禁已完成 compileall、直接契约测试、相关定向回归和全量回归；上述 `703 passed` 是当前最新代码基线。

本次已重新验证前端 TypeScript/Vite production build、`pip check`、Alembic 单一 head 与 `git diff --check`。前端仅有主 chunk 超过 500 kB 的非阻断告警。

默认 skipped 场景分别是真实 DeepSeek/Qwen/Qwen-VL 探针、PostgreSQL checkpoint 单库恢复、MySQL+PostgreSQL 双存储恢复、MySQL smoke、显式 opt-in 的删除集成测试、MySQL Job 租约多 Session，以及同邮件 Graph 派发双 Session 集成测试；当前缺少对应 URL/开关，不可解释为生产环境已通过。

暂停前最后一次本地发布审计快照生成于 `2026-08-13T05:33:06Z`：schema v2，SHA-256 为 `1f294c38795eff25f12ab94380dcb8ee97b50c770347f0fa884d088445a434c8`。该快照只证明默认请求的静态配置检查通过；其 `workflow_engine=legacy`、三个真实基础设施 probe 均未请求、`local_graph_release_gate_passed=false`、`production_signoff_complete=false`。下次不得把该快照作为 Graph 发布证据，应在 clean commit 和目标测试基础设施上重新执行三项 probe 并生成新证据。

## 4. 当前未完成事项

### 4.1 代码与测试侧

本地代码收口已经完成：

1. AI 调用静态架构契约已加入 `tests/test_ai_architecture_contract.py`，限制厂商 SDK、模型构造、structured output 和原始模型 HTTP/JSON 解析边界。
2. Graph Start/重复 Resume 已对 `WorkflowExecution` checkpoint ID/单调 step、PostgreSQL 快照形态及 `WorkflowInterrupt` 账本执行 fail-closed 对账；不会因稳定状态提前返回而掩盖 checkpoint 丢失或重放副作用。
3. 全量后端、编译、依赖、Alembic 单 head、前端 build 和 diff 检查均已重新通过。
4. 旧 `.doc/.xls` 仍是明确的 unsupported/HITL，而不是已实现能力。
5. 灰度稳定并证明 legacy 运行时引用为零之前，不删除 `reparse_email` 迁移期编排分支。
6. DeepSeek 子 Agent 复核发现普通回复真实 Service 返回与 Graph adapter/router 的测试契约不一致；现已统一为顶层 `status/reply_id/send_status`，删除运行时对旧嵌套测试形状的兼容，并新增真实返回形状贯通 adapter、router、HITL action 的直接测试。定向回归 `119 passed`，全量回归提升为 `698 passed, 7 skipped`。
7. DeepSeek 第二轮复核发现 `poll_sap` 内部 reconcile 会恢复 legacy Job 调度，且真实 `pending` 无 Graph 出口。现已补齐 Graph 专用调度关闭开关、`pending → submit_sap` 有界边与上限转人工；跨两次 Interrupt/Resume 测试证明只安全重提一次。SAP/Graph 定向回归 `131 passed`，全量基线提升为 `702 passed, 7 skipped`。
8. DeepSeek 最后一轮复核发现 Graph 人工 `validate` 未传 legacy 校验所需的 `resolving_task_id`，会让已解决的 missing/conflict 标记残留并在 RMA PDF 阶段再次拒绝。现已将验证回调统一为显式 request 契约：仅人工 `action=validate` 传真实 task ID，自动路径传 `None`。定向回归 `68 passed`，全量基线提升为 `703 passed, 7 skipped`。

### 4.2 已完成本地验收的 Job 租约改造

问题来源：后台 Job 原先只在 claim 时写一次 `locked_at`。当 LangGraph Start/Resume 或外部调用运行超过 `ASYNC_JOB_STALE_SECONDS` 时，其他实例可能把仍在执行的 Job 当作 stale 并重新领取，造成同一 checkpoint 分支并发执行。外部副作用幂等不能单独消除 checkpoint 并发推进风险。

已完成：

1. `claim_next_job` 为每次领取生成唯一 `locked_by` fencing token，而不是只记录固定 worker 名称。
2. 新增 `renew_job_lease`，仅当 Job 仍为 `running` 且 token 匹配时续租。
3. 新增 `_lock_owned_job` / `JobLeaseLost`；执行完成或异常落账前重新锁行并核对 token，旧 worker 失去租约后不得覆盖新 owner 的 Job 状态。
4. worker 启动独立 heartbeat session；续租失败或 token 失配时取消当前执行并回滚本地 session。
5. `ASYNC_JOB_STALE_SECONDS` 增加 30 秒下限，保证 stale 窗口内至少三次 heartbeat 机会。
6. 普通 Job 继续 stale 自动恢复；Graph Start/Resume 的 lease 过期因 checkpoint 并发风险进入 `needs_manual_review`，不自动重放。
7. 新增仅管理员可调用的 `POST /api/v1/jobs/{job_id}/retry-stale-graph`；只有明确确认旧 worker 已停止后才重排，并记录确认人、时间和脱敏原因。
8. 新增 `tests/test_job_leases.py`，覆盖 token 唯一性、条件续租、执行前/落账前 fencing、heartbeat 失租、取消 rollback、成功 commit、stale fail-closed 和显式恢复。
9. 当前租约/API 定向回归 `88 passed`；新增发布探针与证据语义单测后，全量回归 `623 passed, 6 skipped`。

尚需目标 MySQL 多实例环境验证 heartbeat 更新与最终落账的实际锁等待、进程强杀和管理员恢复流程；这属于下一节基础设施验收，不影响本地代码验收结论。
严格 opt-in 的 `tests/test_job_lease_integration_mysql.py` 已准备完成：只接受 localhost `repair_system_test`，测试库存在其他活动 Job 时拒绝执行，并在 finally 清理唯一测试 Job 与系统事件。相同探针可由 `python -m tools.audit_langgraph_release --probe-local-test-job-lease --output <path>` 运行，生成结构化发布证据与 SHA-256 sidecar。报告明确区分 requested checks、本地 Graph release gate 和尚未完成的 production signoff。

### 4.3 已完成本地验收的 HITL 与邮件单活所有权收口

1. Human interrupt 的 `allowed_actions` 由实际 State 上下文生成；Resume/API 在业务写入前校验动作，非法或未公布动作直接冲突返回。
2. 人工任务和回复只要存在 `pending` 或 `resume_queued` interrupt 就保持 Graph 所有权；重复提交、专用 reparse、批准和拒绝不会掉回 legacy，拒绝也会 Resume 原 execution 完成终止路径。
3. Graph 邮件派发先对 Email 执行数据库行锁，再依次检查活跃 execution 与活跃 `graph_start` Job，消除“Job 已排队但 execution 尚未落库”时并发生成第二张 Graph 的窗口。
4. `running`、`waiting_human`、`waiting_external`、`resume_queued`、`failed` execution，以及 `queued`、`retry_wait`、`running`、`needs_manual_review`、`failed` 的启动 Job 会占有邮件；完成/取消记录不阻止后续显式重解析。
5. `graph_start` Job ID 写入 `WorkflowExecution.trigger_job_id`，execution 重入对不同 Job 身份 fail-closed，便于追踪及恢复。
6. 管理员可通过 `POST /api/v1/jobs/{job_id}/retry-failed-graph-start` 复活同一个失败启动 Job；原 execution id、checkpoint thread 和幂等键不变，普通 reparse 不能新开 Graph 绕过恢复。
7. 相关执行账本/HITL/API 定向测试已覆盖失败 execution 从原 checkpoint `next` 继续、恢复时回到 running，以及 Job/execution 反向身份冲突 fail-closed；全量测试 `703 passed, 7 skipped`。
8. 新增严格 opt-in 的 MySQL 双 Session 派发探针，验证 Email 行锁确实阻止第二个并发 `graph_start`；审计工具的本地 release gate 现在要求 checkpoint、Job lease、email dispatch 三项探针全部通过。
9. 发布审计报告升级为 schema v2；默认无探针报告仍只能证明所请求检查通过，不能解释为本地 Graph gate 或生产签核完成。
10. Graph 部署必须提供同一 clean commit 生成的 schema v2 三探针证据及 SHA-256 sidecar。证据目录只读挂载，验证先于 checkpoint 初始化；旧 commit、dirty、篡改、旧 schema、缺探针均终止部署。
11. 失败 execution 没有 checkpoint 时禁止从头重放；只能保持 failed 并调查 MySQL/外部台账和 PostgreSQL checkpoint 状态。
12. 部署证据最多有效 168 小时，JSON/sidecar 解析后必须位于只读可信根；过期、未来时间、路径逃逸及 CLI 误用均拒绝。
13. FastAPI lifespan 在任何 scheduler/内置 worker 启动前重复验证证据；直接 Compose/Kubernetes 启动也必须注入 `APP_RELEASE_COMMIT`。管理员 runtime-status 可查看当前进程启动时的 gate 摘要。

### 4.4 必须在目标基础设施完成的发布验收

1. 备份目标 MySQL，执行并审计 migration `r5m0h1c2d3e4`。
   migration 后运行 `python -m tools.check_sap_schema`，再仅在 `repair_system_test` 上设置 `RUN_DELETE_INTEGRATION_TESTS=1` 并运行 `python -m pytest -q tests/test_deletion_integration_mysql.py`。
2. 初始化独立 PostgreSQL checkpointer（`python -m tools.setup_langgraph_checkpoint`），核验最小权限、TLS、静态加密、连接池、备份与恢复。Compose 拓扑已加入，但当前主机没有 Docker CLI，尚未进行容器实测。
   配置 localhost 且数据库名以 `_test` 结尾的 `LANGGRAPH_CHECKPOINT_SMOKE_DATABASE_URL` 后，可运行 `python -m pytest -q tests/test_checkpoint_postgres_integration.py` 验证真实跨连接恢复。
   发布配置审计命令：`python -m tools.audit_langgraph_release --probe-local-test-checkpoint`。
3. 在非生产 SAP/SMTP/OSS 做进程强杀、外部成功但 checkpoint 未写、`submit_unknown` 对账、部分成功和并发 Resume 演练。
   先在 localhost `repair_system_test` 设置 `RUN_JOB_LEASE_INTEGRATION_TESTS=1` 运行 Job 租约测试；再在无 worker 的独立窗口设置 `RUN_EMAIL_DISPATCH_INTEGRATION_TESTS=1` 运行 `tests/test_email_dispatch_integration_mysql.py`，取得真实跨 Session 派发锁证据。
4. 用脱敏真实邮件和 PDF/DOCX/XLSX/图片/ZIP/RAR/7Z 金标运行 shadow 对照。
   Graph 模式下金标工具已要求逐封 v2 execution/checkpoint/interrupt 证据，并成对清理测试 execution/checkpoint；运行结果仍需业务签收。
5. allowlist → 小比例 → 扩大比例 → 全量切换，并签收分类、字段、状态、人工任务和副作用差异。
6. 全量稳定后才做旧编排引用清理与删除。

## 5. 下次建议第一批动作

1. 执行 `git status --short`，确认当前工作树仍完整；不要清理未跟踪文件。
2. 阅读文档 08、09、10，确认没有新的用户范围变更。
3. 运行以下快速回归确认工作树和执行所有权边界仍完整：

```powershell
cd D:\test_graph\repair-mail-agent\backend
$env:PYTHONPATH='.'
python -m pytest -q tests/test_authoritative_email_ticket_graph.py tests/test_workflow_executions.py tests/test_job_leases.py tests/test_graph_resume_job_recovery.py tests/test_api_contract.py
```

4. 若用户提供目标 MySQL/PostgreSQL/SAP/SMTP/OSS 测试环境授权，转入基础设施迁移与恢复演练；优先验证 MySQL 多实例 Job heartbeat/stale/显式恢复，再执行 PostgreSQL 双存储恢复和外部副作用故障注入。未授权时不得写真实外部系统。

## 6. 风险与操作边界

- 当前工作树很脏是本轮正常状态，其中既有修改和新增文件均应保留。
- 不执行 `git reset --hard`、`git checkout --` 或批量删除。
- 不发送真实客户邮件，不向真实 SAP/生产中转库写测试数据，不调用大量付费模型。
- MySQL 业务事实必须先于 PostgreSQL checkpoint 持久化；恢复时仍依赖 Service 幂等 marker/operation ledger。
- `send_uncertain` 和 SAP `submit_unknown` 不得盲目重试。
- 回滚只停止新流量进入 Graph；已产生副作用的 Graph execution 必须原路恢复或转人工，不能切回 legacy 重做。

## 7. 暂停时结论

代码层的主架构、核心自动化闭环和最终本地收口均已落地。剩余工作集中在目标基础设施上的 migration、checkpointer、双存储/外部副作用故障恢复、真实模型/附件金标和灰度签收，以及全量稳定后的 legacy 清理。只有这些外部验收与旧调用清理完成后，才能认定“LangGraph 成为生产唯一编排层”的最终目标完成。
