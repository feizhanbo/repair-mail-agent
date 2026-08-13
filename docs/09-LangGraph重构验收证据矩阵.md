# LangGraph 重构验收证据矩阵

> 更新日期：2026-08-13  
> 说明：本表区分“代码与自动化测试完成”和“目标基础设施/真实外部系统已签收”。后者未执行时，不得宣称生产重构完成。

## 1. 核心验收矩阵

| 验收域 | 当前结论 | 代码证据 | 自动化证据 | 尚需外部签收 |
| --- | --- | --- | --- | --- |
| 主流程唯一编排 | `WORKFLOW_ENGINE=langgraph` 时，生产模块仅暴露一张权威 Active Graph，从邮件解析准备编排至回复/RMA 完成；早期 Active 迁移切片只存在于测试夹具；legacy 仍作为默认回滚入口 | `backend/app/workflows/email_ticket/active_graph.py`、`backend/app/services/jobs.py`、`backend/app/api/v1/emails.py` | 权威 Graph 路径测试、单一生产 Active Graph 静态契约 | 灰度完成后搜索并删除 legacy 编排分支 |
| Graph State 解耦 | State 仅存 JSON-safe 标识、阶段结果、路由和错误，不存 ORM/Session | `backend/app/workflows/email_ticket/state.py` | Graph/重启测试 | 目标 PostgreSQL checkpoint 数据抽检 |
| Node/Service 解耦 | Node 只组装 State delta 并调用 Runtime 注入的现有 Service adapter；Side Effect Node 缺少 Service 时 fail-closed | `nodes.py`、`external.py`、`human.py`、`adapters.py` | `test_email_ticket_nodes.py`、`test_workflow_service_adapters.py` | 无 |
| Conditional Router | 分类、附件、校验、SAP、RMA、回复和人工恢复均由确定性 Router 路由；统一 error 短路到人工 | `routers.py`、文档 07 节点/路由表 | `test_email_ticket_routers.py` 全分支表驱动测试、Graph 路径测试 | 金标场景差异签收 |
| LangChain 模型层 | DeepSeek/Qwen/Qwen-VL 由统一 ModelSpec、Gateway、structured output、错误与 usage 映射调用；静态架构契约禁止业务代码重新引入厂商 SDK、模型构造、原始模型 HTTP endpoint 或响应 `json.loads` | `backend/app/ai/`、`integrations/*provider.py` | AI gateway/provider/routing/regression、静态契约，以及默认跳过的三模型真实 structured-output 探针 | 在受控配置下显式运行 `RUN_REAL_AI_INTEGRATION_TESTS=1` 并签收 |
| LLM 能力边界 | LLM 只做分类、抽取、非结构化/视觉理解和草稿；SN、客户、状态、事务、SAP、SMTP 等保持确定性 | AI service、ticket safety、SAP/RMA/reply services | AI mock、ticket safety、side-effect 测试 | 业务金标抽检 |
| 附件分层 | Raw bytes → detector/safety/parser → normalized content → 条件式 Qwen/Qwen-VL → 业务抽取 | `attachments/`、`attachment_parser.py` | attachment parser/safety 测试 | 真实七类附件样本验收 |
| DOCX | 确定性提取段落、表格并计数/提取内嵌图片；仅含图时调用 VL，并传入文本/表格上下文 | `attachment_parser.py` | DOCX 结构及含图 VL 路由测试 | 复杂 Word 样本（合并单元格、关系损坏）抽检 |
| XLSX | openpyxl 按 workbook/sheet/row/cell 确定性读取，限制 sheet/row/column | `attachment_parser.py` | XLSX parser 测试 | 大表/公式样本抽检 |
| PDF | 先提取文本并检测页数、表格和页面图片；扫描件/混合图文渲染后调用 VL，纯文本/表格走文本；加密/损坏转人工 | `attachment_parser.py` | 文本、扫描、混合图文 PDF 路由测试 | 真实复杂表格与混合图文样本抽检 |
| 图片 | 图片和非装饰性 inline image 才调用 Qwen-VL；小签名图确定性跳过 | `attachment_parser.py` | image/inline 测试 | 真实拍照、旋转和低清样本抽检 |
| 压缩包 | ZIP/RAR/7Z/TAR/GZIP 元数据安全检查、受限解包、递归路由；加密/损坏/穿越/炸弹转人工 | `attachments/safety.py`、`attachments/archive.py` | ZIP/TAR/GZIP/7Z/嵌套/加密测试 | RAR 后端与真实 RAR 样本签收 |
| 旧 `.doc/.xls` | 邮件附件链历史上未实现；保持 unsupported/HITL。`xlrd` 仅用于主数据导入，不虚构邮件支持 | `attachments/detector.py`、`services/master_data.py` | unsupported 路径测试 | 如业务需要，另立兼容迁移需求 |
| Human-in-the-loop | Graph 创建/复用人工任务，按 State 生成可执行动作，`interrupt()` 暂停；API 只接受 interrupt 公布的动作并记录结构化决定，`Command(resume=...)` 恢复。`pending`/`resume_queued` 均保持 Graph 所有权，重复 resolve、reparse、reply approve/reject 不回退 legacy；checkpoint ID + 单调 step 对账避免旧 payload 二次注入及 checkpoint 回退误判；确定性终止失败释放 resume 租约，worker 租约过期则保持 fail-closed | `human.py`、`runner.py`、`workflow_execution.py`、jobs/manual/reply APIs | HITL 动作矩阵、非法动作、Graph 绑定专用 reparse、approve/reject、重复提交、payload、checkpoint 前进/未前进/缺失/回退、终止 Job 释放/复活、stale Graph Job 显式恢复测试 | 多实例并发人工处理演练 |
| Checkpointer | PostgreSQL 保存 Graph 上下文；MySQL 的 execution 与 interrupt 均保存 checkpoint identity。稳定 execution 的 Start/重复 Resume 必须核对 ID、单调 step、execution identity、next/interrupt 形态和 interrupt 账本；缺失、回退、未入账前移、分叉或冲突均 fail-closed 且不重放 Graph。Compose 通过 `langgraph` profile 提供独立 PostgreSQL，legacy 不启动；严格 serializer 禁用 pickle fallback | checkpointer factory、`runner.py`、workflow ORM、Compose、setup/audit tool、migration `r5m0h1c2d3e4` | checkpoint 单调性、稳定状态/账本冲突、重复 Resume、部署/发布审计及内存重启测试 | 目标 PostgreSQL setup、权限、备份恢复和双存储故障注入演练 |
| Job 租约与并发 fencing | 每次 claim 使用唯一 owner token，heartbeat 条件续租；执行前/落账前核验 token，丢租约取消并 rollback。Graph stale 不自动重放，管理员确认旧 worker 已停止后才能显式重排；邮件派发在 Email 行锁内复用活跃 execution/启动 Job，覆盖 execution row 尚未创建的排队窗口，`trigger_job_id` 固化启动身份。失败 Graph 继续占有邮件，管理员只能复活同一个失败启动 Job，不能以 reparse 绕过原 checkpoint/副作用恢复 | `backend/app/services/jobs.py`、`backend/app/services/emails.py`、`backend/app/workflows/executions.py`、`backend/app/main.py`、jobs API、config、release audit tool | `test_job_leases.py`、邮件单活派发/失败所有权/触发 Job fencing/恢复 API、workflow config、P0、Graph Resume recovery、API contract；另有默认跳过的 localhost MySQL Job lease 与同邮件双 Session 派发集成测试 | 在无 worker 的 `repair_system_test` 显式运行两个 MySQL 集成探针，再做同邮件并发 reparse、多实例锁等待、进程强杀和管理员恢复演练 |
| Workflow 审计删除兼容 | 删除邮件、工单、Job、人工任务或恢复用户时保留 execution/interrupt 并置空业务关联；删除 execution 时级联 interrupt | workflow ORM、migration `r5m0h1c2d3e4`、`check_sap_schema.py` | ORM 外键契约、离线迁移 SQL、opt-in MySQL 删除集成场景 | 在迁移后的 `repair_system_test` 实际执行删除集成测试 |
| SAP 幂等与恢复 | submit/reconcile/poll 分离；不确定提交只对账不盲重提；确认远端不存在后每个 export 最多安全重提一次，再次 pending 转人工；Graph 等待使用持久化 interrupt | `sap_rma.py`、`external_operations.py`、Graph State/Router | SAP/RMA restart/replay、bounded safe-resubmit、新 export 计数重置测试 | 非生产 SAP 故障注入 |
| RMA/PDF/OSS 幂等 | 生成、发送、归档拆分；复用 reply/operation ledger 和稳定业务标识 | `replies.py`、`rma_pdf.py` | RMA side-effect boundary 测试 | 非生产 OSS 超时/部分成功演练 |
| SMTP 幂等 | 稳定 Message-ID、发送台账、`send_uncertain` 禁止自动重发；发送与归档分离 | `replies.py`、`external_operations.py` | reply/RMA side-effect 测试 | 测试 SMTP 进程强杀与重复恢复演练 |
| 异常体系 | 可纠正 4xx、LookupError、ValueError 映射统一 State error/HITL；系统异常交 Job 重试 | `errors.py` | authoritative Graph error 测试 | 外部超时/断连故障注入 |
| 可观测性 | execution 最终节点/路由、每个非 interrupt Node 的 started/result/duration/error/route delta、AI model/tokens、Job retry、外部 operation、human interrupt 均可关联；runtime-status 暴露 Graph waiting/failed、pending interrupt error 计数及当前进程启动时固化的 release gate engine/verified/schema/commit/SHA 摘要 | `observability.py`、`workflow_execution.py`、system runtime API、现有 logs/ledgers | observability、Graph Job recovery、runtime gate snapshot、API contract 测试 | 日志平台查询、脱敏和告警签收 |
| 灰度与回滚 | allowlist + 稳定百分比分桶；回复/人工入口按实际 interrupt 绑定判断 Graph 所有权，legacy 灰度流量不被全局开关截断；默认 legacy；金标在 Graph 模式逐封签收 v2 execution/checkpoint/interrupt，reset 成对清理并验证测试 checkpoint 与 execution 无残留；Graph 部署及应用 lifespan 均验证同一 clean commit、168 小时内的 schema v2 三探针证据和 SHA-256，解析路径必须位于只读可信根，且在 checkpoint/runtime config/RMA/scheduler 初始化前 fail-closed；生产回滚不清 checkpoint/外部台账 | config、application lifespan、deploy/Compose、release evidence/audit、jobs、reply/manual APIs、gold replay Service/tool、文档 05/07/08 | workflow config、动态 lifespan fail-fast、部署顺序/只读挂载、证据篡改/dirty/旧 commit/过期/未来时间/路径逃逸/旧 schema/缺探针拒绝、Graph/legacy 归属、金标证据与清理契约测试 | shadow 金标、灰度指标和回滚演练 |

## 2. 当前自动化结论

```text
python -m pytest -q
703 passed, 7 skipped, 12 warnings
```

本轮相关执行账本/HITL/邮件所有权定向回归为 `128 passed, 2 warnings`。静态编译、`pip check`、Alembic 单一 head `r5m0h1c2d3e4`、前端 TypeScript/Vite production build 与 `git diff --check` 同时通过。

另已新增显式 opt-in checkpoint 集成测试。第一场只接受 localhost 且 PostgreSQL 数据库名以 `_test` 结尾，验证跨连接、跨 Graph 重编译的 interrupt/resume 及已完成节点不重放；第二场同时要求 MySQL `repair_system_test`，验证真实 execution/interrupt 与 checkpoint 的双存储对账。两场均清理测试 thread/ledger。当前主机没有目标测试数据库，因此两场均 skipped。

默认跳过的外部场景包括：真实 DeepSeek/Qwen/Qwen-VL 探针、PostgreSQL checkpoint 单库恢复、MySQL+PostgreSQL 双存储恢复、MySQL smoke、删除集成测试、MySQL Job 租约多 Session，以及同邮件 Graph 派发双 Session 集成测试。它们不计为生产验证。当前自动化没有发送真实客户邮件、没有写真实 SAP/生产中转数据库、没有执行真实付费模型调用。

## 3. 生产完成前的阻塞清单

1. 在目标 MySQL 备份后执行并审计 migration `r5m0h1c2d3e4`。
   migration 后先执行 `python -m tools.check_sap_schema`；再仅对 `repair_system_test` 设置 `RUN_DELETE_INTEGRATION_TESTS=1`，验证新增 workflow 外键不会阻断现有物理删除流程。
2. 初始化独立 PostgreSQL checkpointer，验证最小权限、TLS/静态加密、备份和恢复。
   初始化后先运行 `python -m tools.audit_langgraph_release --probe-local-test-checkpoint`；真实恢复测试使用 `tests/test_checkpoint_postgres_integration.py`。
3. 在非生产 SAP/SMTP/OSS 上执行“外部成功但 checkpoint 未写”、进程强杀、`submit_unknown` 对账和并发 Resume 演练。
   先在 localhost `repair_system_test` 设置 `RUN_JOB_LEASE_INTEGRATION_TESTS=1` 运行 Job lease 测试；再在无 worker 的独立测试窗口设置 `RUN_EMAIL_DISPATCH_INTEGRATION_TESTS=1` 运行 `tests/test_email_dispatch_integration_mysql.py`。最终执行 `python -m tools.audit_langgraph_release --probe-local-test-job-lease --probe-local-test-email-dispatch --output ../test-results/langgraph-release/mysql-concurrency.json` 留存带 SHA-256 sidecar 的 JSON 证据。`local_graph_release_gate_passed` 还要求 checkpoint 探针通过，且与 `production_signoff_complete` 必须按限定语义解释。
4. 使用真实脱敏邮件及 PDF/DOCX/XLSX/图片/ZIP/RAR/7Z 金标运行 shadow 对照并签收差异。
   复用现有 `tools/run_gold_mail_regression.py`；该工具按最终邮件/工单/出站事实轮询，并在 Graph 模式核验每封邮件的 v2 execution/checkpoint/interrupt。reset 会成对清理测试 execution 与 checkpoint，但必须在独立 localhost 测试部署中明确设置目标 workflow engine。
5. allowlist → 小比例 → 扩大比例 → 全量，持续核对状态、人工任务、外部台账和重复副作用指标。
6. 全量稳定且全局引用搜索为零后，才删除迁移期 legacy 编排代码；legacy 回滚开关不能提前移除。
