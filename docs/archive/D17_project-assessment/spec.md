# 邮件报修自动化系统项目现状评估 Spec

## Why
项目需要一份系统性的技术现状评估报告，帮助后续开发人员快速接手项目，并为AI编程助手提供明确的推进方向。

## What Changes
- 对项目整体架构、技术栈、功能模块进行全面分析
- 评估各模块完成度与开发进度
- 梳理项目运行条件与启动方式
- 识别阻塞问题与风险点
- 输出按优先级排序的后续行动清单

## Impact
- Affected specs: 无（新项目评估，不修改现有功能）
- Affected code: 无（仅分析，不修改代码）

---

## 项目概况

### 项目名称
`repair-mail-agent` —— 邮件报修自动化系统

### 项目定位
面向客户邮件报修场景的内部试运行系统（一期），目标是将客户邮件报修流程从人工识别、登记、追问、SN校验，升级为可追溯的自动化闭环。

### 核心问题
解决从报修邮箱自动拉取客户邮件、归档原始EML与附件、AI辅助解析报修字段、SN校验、自动追问、人工复核、工单管理、回复审核的全链路自动化问题。

### 技术架构
- **后端**: Python 3.11 + FastAPI, SQLAlchemy 2.x asyncio, Pydantic Settings, Alembic
- **数据库**: MySQL 8.x, utf8mb4, DATETIME(3)
- **AI**: DeepSeek OpenAI兼容接口, Provider抽象, JSON输出校验
- **前端**: React + TypeScript + Vite, Ant Design, TanStack Query, Zustand
- **部署预留**: Docker Compose, Nginx, GitHub Actions

### 主要功能模块
| 模块 | 说明 |
|------|------|
| 认证与权限 | JWT登录, RBAC角色控制 (admin/supervisor/operator) |
| 邮件中心 | 邮件入库、列表查询、详情查看、重新解析、线程合并/拆分 |
| 工单中心 | 工单生成、字段编辑、明细管理、SN校验、状态流转、审计日志 |
| 解析引擎 | 规则解析 + DeepSeek AI增强解析, 解析候选采纳/拒绝 |
| 人工复核 | 任务队列、认领/分配/释放/处理、字段修正、证据面板 |
| 回复审核 | 追问草稿生成(AI+模板)、人工审核、发送开关控制 |
| 基础资料 | SN/资产库、板卡/物料寄北京规则导入与管理 |
| AI日志 | 调用摘要索引, JSONL完整日志定位 |
| 通知系统 | 站内消息, SSE预留 |
| 系统配置 | 配置信息查看, 工作流状态与转换规则展示 |

---

## 开发模块完成度分析

### 后端API完成度

| 模块 | 端点数量 | 完成度 | 说明 |
|------|---------|--------|------|
| 认证 auth | 4 | 100% | 登录/获取当前用户/更新资料/修改密码 |
| 用户管理 users | 7 | 100% | CRUD + 状态管理 + 角色分配 + 密码重置 |
| 看板 dashboard | 1 | 100% | 首页汇总统计 |
| 邮件 emails | 4 | 80% | 入库/列表/详情/重解析已完成, IMAP自动拉取未实现 |
| 线程 email-threads | 2 | 100% | 合并/拆分线程 |
| 工单 tickets | 11 | 90% | CRUD/字段编辑/状态流转/SN校验/解析应用/时间线/证据 |
| 解析 parse-results | 1 | 100% | 采纳解析结果 |
| 人工复核 manual-review | 7 | 100% | 任务列表/详情/认领/分配/释放/处理/重解析 |
| 回复 replies | 5 | 70% | 草稿/编辑/审核已完成, SMTP真实发送未实现 |
| 基础资料 master-data | 4 | 100% | SN资产/板卡列表与导入 |
| AI日志 ai-logs | 1 | 100% | 调用日志列表 |
| 通知 notifications | 3 | 60% | 列表/标记已读已完成, SSE推送为占位实现 |
| 系统 system | 1 | 100% | 系统配置信息查看 |
| 健康检查 health | 1 | 100% | 健康检查端点 |

**总计: 约45个端点, 平均完成度约90%**

### 前端页面完成度

| 页面 | 路由 | 完成度 | 说明 |
|------|------|--------|------|
| 登录页 | /login | 100% | 用户名密码登录, 已登录自动跳转 |
| 首页看板 | / | 100% | 6指标卡片 + 最近异常任务, 60s自动刷新 |
| 邮件中心 | /emails | 95% | 列表/详情/手动入库/重解析, 缺批量操作 |
| 工单中心 | /tickets | 95% | 全生命周期管理, 9个Tab详情面板 |
| 人工复核 | /manual-review | 95% | 三栏工作台, 任务队列+证据+操作面板 |
| 自动回复审核 | /replies | 90% | 草稿生成/审核通过/驳回 |
| 基础资料 | /master-data | 90% | SN资产+板卡规则两个Tab, JSON导入 |
| 用户管理 | /users | 90% | CRUD+状态+角色+密码重置(仅admin) |
| 个人信息 | /profile | 100% | 查看/编辑资料+修改密码 |
| 站内消息 | /notifications | 90% | 列表/筛选/标记已读/详情/跳转关联 |
| AI日志 | /ai-logs | 90% | 筛选列表+行展开详情(仅supervisor/admin) |
| 系统配置 | /system | 80% | 配置信息+状态定义+流转规则(仅supervisor/admin) |

**总计: 12个页面, 平均完成度约92%**

### 数据库完成度

26张业务表 + alembic_version表, ORM模型和迁移均已完成。最新迁移: 9d2b7c4f1a30。

### 核心业务流程完成度

| 流程步骤 | 完成度 | 说明 |
|---------|--------|------|
| 邮件入库 | 70% | 手工入库完成, IMAP自动拉取未实现 |
| 原文归档 | 30% | OSS模型就绪, 实际OSS上传/下载代码未实现 |
| 回复链识别 | 80% | 线程归并逻辑完成, 实际IMAP收信中的In-Reply-To解析未经过联调 |
| 规则解析 | 90% | 规则解析引擎完成, HTML清洗/字段提取/意图分类 |
| AI解析 | 85% | DeepSeek Provider完成, JSON校验/失败回退/日志完整 |
| 工单生成 | 90% | 从解析结果创建工单完成, 状态流转/乐观锁/审计完成 |
| SN校验 | 85% | 资产库匹配逻辑完成, 校验结果记录完成 |
| 人工复核 | 90% | 任务创建/认领/分配/处理闭环完成 |
| 追问草稿 | 80% | AI+模板生成草稿完成, SMTP真实发送未实现 |
| 回复审核 | 80% | 审核通过/驳回完成, SMTP真实发送未实现 |
| 站内通知 | 70% | 通知创建/列表/标记已读完成, SSE推送为占位 |

---

## 项目运行可行性判断

### 当前状态: 需要补充配置后可以运行

### 判断依据

| 检查项 | 状态 | 说明 |
|--------|------|------|
| Python 3.11+ | ✅ 已安装(3.11.6) | |
| Node.js | ✅ 已安装(18.19.0) | |
| Docker | ❌ 未安装 | 无法使用Docker Compose启动 |
| MySQL数据库 | ❌ 未运行 | 需要本地或远程MySQL实例 |
| .env文件 | ❌ 不存在 | 依赖默认配置, 数据库连接指向127.0.0.1:13307(SSH隧道) |
| 后端依赖 | ✅ 已安装 | requirements.txt全部满足 |
| 前端依赖 | ❌ 未安装 | npm install因registry问题失败 |
| 后端编译 | ✅ 通过 | python -m compileall app 全部成功 |
| 后端测试 | ✅ 32 passed | 全部测试通过 |
| 前端类型检查 | ❌ 未运行 | node_modules未安装 |
| 数据库迁移 | ⚠️ 未执行 | 需要MySQL实例和Alembic运行 |
| 种子数据 | ⚠️ 未执行 | 需要MySQL实例 |

### 运行环境要求

| 项目 | 当前值 | 要求 |
|------|--------|------|
| Python | 3.11.6 | 3.11+ |
| Node.js | 18.19.0 | 18+ |
| npm | 10.2.3 | 9+ |
| MySQL | 未运行 | 8.0+ |
| Docker | 未安装 | 可选(Docker Compose部署时) |
| Redis | 未运行 | 配置存在但代码中未使用 |

---

## 关键问题清单

### 阻塞运行问题(P0)

| 编号 | 问题 | 影响 |
|------|------|------|
| P0-01 | 无可用MySQL数据库实例 | 后端无法启动 |
| P0-02 | 无.env配置文件 | 数据库连接/密钥均为占位符 |
| P0-03 | Redis配置存在但无Redis服务 | 如代码使用Redis会崩溃(当前未使用) |
| P0-04 | Docker未安装 | 无法使用Docker Compose部署 |

### 影响联调问题(P1)

| 编号 | 问题 | 影响 |
|------|------|------|
| P1-01 | SMTP真实发送未实现 | 回复审核后可审核但无法真实发送邮件 |
| P1-02 | IMAP自动拉取未实现 | 邮件需手工入库, 无法自动收取 |
| P1-03 | OSS文件存储未实际对接 | 附件/EML无法上传到OSS |
| P1-04 | SSE通知流为占位实现 | 站内通知无法实时推送 |
| P1-05 | APScheduler未使用 | 无定时任务(IMAP拉取/数据导入等) |
| P1-06 | Repositories层为空 | 未实现Repository模式 |
| P1-07 | Workers层为空 | 无后台任务消费者 |

### 影响开发效率问题(P2)

| 编号 | 问题 | 影响 |
|------|------|------|
| P2-01 | deploy.sh缺少关键步骤(备份/迁移/健康检查) | 部署不安全 |
| P2-02 | Nginx缺少安全头/gzip/body_size限制 | 性能和安全问题 |
| P2-03 | 后端容器无healthcheck | Docker无法感知服务状态 |
| P2-04 | CI/CD无测试门禁 | 有bug的代码可能被部署 |
| P2-05 | 前端两个大型组件(ManualReviewPage 866行/TicketsPage 741行) | 可维护性问题 |
| P2-06 | 前端npm registry不可用 | 初次开发者无法安装依赖 |

### 文档与代码差异

| 编号 | 差异 | 文档要求 | 代码现状 |
|------|------|---------|---------|
| D-01 | deploy.sh与标准流程不一致 | 9步标准部署流程 | 仅2步(拉代码+重建容器) |
| D-02 | REDIS_URL在.env.example中定义 | 存在Redis配置 | docker-compose无Redis服务, 代码也未使用 |
| D-03 | Repositories层在文档中规划 | 要求Repository模式 | 实际为空(.gitkeep) |

---

## 项目运行方式

### 环境准备

```bash
# 1. Python环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
.venv\Scripts\activate     # Windows

# 2. Node.js环境 (已安装 Node 18+)

# 3. MySQL 8.0+ 实例 (需自行启动或配置远程连接)

# 4. 创建 .env 文件
cp .env.example .env
# 编辑 .env 填入真实配置
```

### 依赖安装

```bash
# 后端
cd backend
pip install -r requirements.txt

# 前端
cd frontend
npm install
```

### 环境变量配置

必须修改的 `.env` 配置项:
- `DATABASE_URL` - MySQL连接字符串
- `JWT_SECRET` - JWT签名密钥(生产必须使用强随机值)
- `AI_API_KEY` - DeepSeek API Key(如需AI功能)
- `MYSQL_ROOT_PASSWORD` - MySQL root密码
- `DEFAULT_ADMIN_PASSWORD` - 默认管理员密码

可选配置(当前代码未使用但已预留):
- `IMAP_*` / `SMTP_*` - 邮件收发配置
- `OSS_*` - 阿里云OSS配置

### 数据库初始化

```bash
cd backend
# 执行迁移
alembic upgrade head
# 初始化种子数据(角色/管理员/状态/模板)
python -m app.seed
```

### 启动命令

```bash
# 后端
cd backend
uvicorn app.main:app --reload

# 前端
cd frontend
npm run dev

# Docker Compose (需要Docker和.env文件)
docker compose up -d
```

### 服务访问地址

| 服务 | 本地开发 | Docker |
|------|---------|--------|
| 前端 | http://localhost:5173 | http://localhost |
| 后端API | http://localhost:8000 | http://localhost/api/v1 |
| API文档 | http://localhost:8000/docs | http://localhost/api/docs |
| MySQL | localhost:3306 | 127.0.0.1:3307 |

### 常见启动失败原因

1. **MySQL连接失败**: 检查DATABASE_URL和MySQL服务状态
2. **JWT_SECRET未设置**: 生产环境必须修改默认值
3. **前端npm install失败**: registry不可用, 尝试切换到npm官方源
4. **端口冲突**: 8000(后端)/5173(前端)/3306(MySQL)被占用
5. **.env文件缺失**: 系统使用config.py默认值, 但数据库连接指向127.0.0.1:13307(SSH隧道)

---

## 测试与验证结果

| 验证项 | 结果 | 说明 |
|--------|------|------|
| 后端compileall | ✅ 通过 | 所有Python文件编译成功 |
| 后端pytest | ✅ 32 passed | 27个models/api/ai/roles/user测试 + 5个额外测试 |
| 前端typecheck | ❌ 未执行 | node_modules未安装 |
| 前端build | ❌ 未执行 | node_modules未安装 |
| 数据库连接 | ❌ 未执行 | MySQL实例不可用 |
| Docker部署 | ❌ 不支持 | Docker未安装 |
