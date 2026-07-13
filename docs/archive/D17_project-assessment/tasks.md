# 邮件报修自动化系统 后续任务清单

## 优先级说明
- **P0**: 必须立即解决，否则项目无法运行
- **P1**: 影响核心功能联调，需要尽快解决
- **P2**: 影响开发效率或稳定性，建议优化
- **P3**: 非阻塞问题，可后续完善

---

## P0 - 必须立即解决

- [ ] P0-01: 准备MySQL数据库实例
  - 问题描述: 当前环境无可用MySQL 8.0+实例，后端无法启动
  - 影响范围: 整个后端服务
  - 推荐方案: 本地安装MySQL 8.0或使用Docker启动MySQL容器, 创建数据库`repair_system_dev`
  - 涉及文件: docker-compose.yml, .env, backend/app/config.py
  - 验收标准: `mysql -h 127.0.0.1 -u root -p` 可成功连接, `CREATE DATABASE repair_system_dev DEFAULT CHARSET utf8mb4` 成功

- [ ] P0-02: 创建.env配置文件
  - 问题描述: 项目根目录无.env文件, 依赖config.py硬编码默认值, 数据库连接指向SSH隧道端口
  - 影响范围: 所有后端配置
  - 推荐方案: 基于.env.example创建.env, 修改DATABASE_URL、JWT_SECRET、DEFAULT_ADMIN_PASSWORD等为实际值
  - 涉及文件: .env.example -> .env
  - 验收标准: .env文件存在于项目根目录, `python -c "from app.config import settings; print(settings.APP_ENV)"` 正确读取配置

- [ ] P0-03: 执行数据库迁移
  - 问题描述: 数据库表结构未创建, 后端无法正常访问数据
  - 影响范围: 所有数据库操作
  - 推荐方案: 在MySQL可用且.env配置正确后, 执行 `alembic upgrade head`
  - 涉及文件: backend/alembic/versions/
  - 验收标准: `alembic current` 显示版本为 `9d2b7c4f1a30`, 数据库中存在26张业务表

- [ ] P0-04: 执行种子数据初始化
  - 问题描述: 缺少角色、管理员、工作流状态等基本数据, 系统无法登录和使用
  - 影响范围: 用户登录、权限控制、工作流
  - 推荐方案: 执行 `python -m app.seed`
  - 涉及文件: backend/app/seed.py
  - 验收标准: roles表有3条记录(admin/supervisor/operator), users表有默认管理员, workflow_statuses和workflow_transitions表有数据

---

## P1 - 影响核心功能联调

- [ ] P1-01: 实现SMTP真实发送功能
  - 问题描述: approve_reply接口仅更新审核状态, 未真实调用SMTP发送邮件
  - 影响范围: 回复审核模块无法完成邮件发送闭环
  - 推荐方案: 在app/integrations/下创建smtp_client.py, 调用smtplib发送邮件, 在replies.py的approve_reply中集成, 受AUTO_SEND_ENABLED开关控制
  - 涉及文件: backend/app/services/replies.py, backend/app/integrations/(新文件)
  - 验收标准: AUTO_SEND_ENABLED=true时审核通过回复后, SMTP真实发送邮件并记录smtp_message_id

- [ ] P1-02: 实现IMAP自动拉取邮件
  - 问题描述: 邮件只能手工入库, 无法自动从报修邮箱收取
  - 影响范围: 邮件入库流程
  - 推荐方案: 在app/integrations/下创建imap_client.py, 在app/workers/下创建邮件拉取定时任务, 使用APScheduler调度
  - 涉及文件: backend/app/integrations/(新文件), backend/app/workers/(新文件), backend/app/services/emails.py
  - 验收标准: 定时任务可自动连接IMAP服务器, 拉取新邮件并入库, 重复邮件自动跳过

- [ ] P1-03: 实现OSS文件存储对接
  - 问题描述: OSS模型和SDK就绪但无实际上传/下载代码
  - 影响范围: 邮件附件存储、原始EML存储
  - 推荐方案: 创建oss_client.py封装阿里云OSS SDK, 实现upload/download/presigned_url, 在邮件入库和附件处理流程中集成
  - 涉及文件: backend/app/integrations/(新文件), backend/app/services/emails.py
  - 验收标准: 邮件入库时原始EML上传到OSS并写入oss_objects表, 附件文件上传到OSS并可获取下载链接

- [ ] P1-04: 实现SSE实时通知推送
  - 问题描述: /notifications/stream仅发送connected事件后挂起, 无实际推送逻辑
  - 影响范围: 站内通知实时性
  - 推荐方案: 使用asyncio.Queue实现消息广播, 通知创建时推送到对应用户的队列, SSE端点从队列读取并推送
  - 涉及文件: backend/app/api/v1/notifications.py, backend/app/services/audit.py
  - 验收标准: 创建人工复核任务时, 对应操作员前端立即收到SSE通知推送

- [ ] P1-05: 启用APScheduler定时任务
  - 问题描述: APScheduler依赖已安装但未定义任何定时任务
  - 影响范围: IMAP拉取、数据导入等需要定时的操作
  - 推荐方案: 在app/workers/下创建scheduler.py, 定义IMAP拉取等定时任务, 在app/main.py启动事件中初始化
  - 涉及文件: backend/app/workers/(新文件), backend/app/main.py
  - 验收标准: 服务启动后定时任务自动注册, IMAP拉取按配置间隔执行

- [ ] P1-06: 实现Repositories层
  - 问题描述: 文档规划了Repository模式但实际为空
  - 影响范围: 代码架构, 数据访问一致性
  - 推荐方案: 为核心聚合(Email/Ticket/Review/User)创建Repository类, 迁移service中的直接session操作到Repository
  - 涉及文件: backend/app/repositories/(新文件)
  - 验收标准: EmailRepository/TicketRepository/ReviewRepository等创建完成, 核心查询通过Repository执行

- [ ] P1-07: 修复前端npm依赖安装
  - 问题描述: 当前环境npm registry不可用(npm install失败)
  - 影响范围: 前端开发环境
  - 推荐方案: 切换npm registry到官方源或可用镜像, 执行npm install
  - 涉及文件: frontend/package.json, .npmrc
  - 验收标准: npm install成功, npm run dev可启动前端开发服务器

---

## P2 - 影响开发效率或稳定性

- [ ] P2-01: 完善deploy.sh部署脚本
  - 问题描述: 当前仅2步(拉代码+重建容器), 缺少数据库备份/迁移/健康检查等关键步骤
  - 影响范围: 生产部署安全性和可靠性
  - 推荐方案: 按照统一开发总结文档9.5节要求, 补齐9步标准部署流程
  - 涉及文件: deploy.sh
  - 验收标准: deploy.sh包含: 1)环境检查 2)git pull 3)数据库备份 4)启动mysql并等待 5)迁移 6)seed 7)重建容器 8)健康检查 9)状态报告

- [ ] P2-02: 强化Nginx配置
  - 问题描述: 缺少gzip压缩/安全头/client_max_body_size/速率限制
  - 影响范围: 性能和安全
  - 推荐方案: 添加gzip压缩、X-Frame-Options/Content-Security-Policy等安全头、client_max_body_size 50m、API速率限制
  - 涉及文件: nginx/default.conf
  - 验收标准: HTTP响应头包含安全头, 静态资源启用gzip, 大文件上传有大小限制

- [ ] P2-03: 后端容器添加healthcheck
  - 问题描述: Docker Compose中backend-api无healthcheck, Docker无法感知服务状态
  - 影响范围: Docker编排可靠性
  - 推荐方案: 在docker-compose.yml为backend-api添加healthcheck(调用/health端点), 或Dockerfile添加HEALTHCHECK指令
  - 涉及文件: docker-compose.yml, docker/backend.Dockerfile
  - 验收标准: docker compose ps显示backend-api为healthy

- [ ] P2-04: CI/CD添加测试门禁
  - 问题描述: GitHub Actions仅执行deploy.sh, 无pytest/typecheck/build验证
  - 影响范围: 代码质量保障
  - 推荐方案: 在deploy.yml中添加pre-deploy步骤: pytest + npm run typecheck + npm run build, 任一步骤失败则停止部署
  - 涉及文件: .github/workflows/deploy.yml
  - 验收标准: 推送代码到main分支后, CI先运行测试, 测试失败不会触发部署

- [ ] P2-05: 重构大型前端组件
  - 问题描述: ManualReviewPage 866行, TicketsPage 741行, 包含多个行内定义的子组件
  - 影响范围: 代码可维护性
  - 推荐方案: 将ManualEvidencePane/ManualActionPane/EmailTimelineItem/TicketDetailView等拆分为独立文件
  - 涉及文件: frontend/src/pages/ManualReviewPage.tsx, frontend/src/pages/TicketsPage.tsx
  - 验收标准: 每个组件文件不超过500行, 子组件在独立目录中

- [ ] P2-06: 添加容器日志卷挂载
  - 问题描述: Nginx和backend-api日志仅存在于容器内, 容器重建后丢失
  - 影响范围: 排障能力
  - 推荐方案: 在docker-compose.yml中添加日志卷挂载: ./logs/nginx:/var/log/nginx, ./logs/app:/app/logs
  - 涉及文件: docker-compose.yml
  - 验收标准: docker compose启动后, logs目录下有nginx和应用日志文件

---

## P3 - 非阻塞问题

- [ ] P3-01: 解决REDIS_URL配置冗余
  - 问题描述: .env.example定义了REDIS_URL但docker-compose无Redis服务, 代码中也未使用
  - 影响范围: 配置清晰度
  - 推荐方案: 如一期不需要Redis, 从.env.example中移除或添加注释说明; 如需要则添加Redis到docker-compose
  - 涉及文件: .env.example, docker-compose.yml

- [ ] P3-02: 添加前端.env文件
  - 问题描述: 前端无.env文件, VITE_API_BASE_URL依赖隐式默认值
  - 影响范围: 前端配置明确性
  - 推荐方案: 在frontend/下创建.env和.env.production, 明确设置VITE_API_BASE_URL
  - 涉及文件: frontend/.env(新文件)

- [ ] P3-03: 扩展测试覆盖
  - 问题描述: 当前27个测试用例集中在模型/API契约/AI Provider, 缺少核心业务流程集成测试
  - 影响范围: 质量保障
  - 推荐方案: 添加email_ingest/ticket_create/workflow_transition/manual_review流程集成测试
  - 涉及文件: backend/tests/

- [ ] P3-04: 容器切换非root用户运行
  - 问题描述: 后端和前端容器均以root运行
  - 影响范围: 安全性
  - 推荐方案: 在Dockerfile中添加USER指令, 创建非root用户运行应用
  - 涉及文件: docker/backend.Dockerfile, docker/frontend.Dockerfile

- [ ] P3-05: 前端添加路由守卫组件
  - 问题描述: 路由保护逻辑仅写在AppLayout中, 子页面无独立的权限校验组件
  - 影响范围: 安全性(UI层面)
  - 推荐方案: 创建ProtectedRoute组件, 在路由配置中包裹需要特定角色的路由
  - 涉及文件: frontend/src/routes/router.tsx, frontend/src/components/ProtectedRoute.tsx(新文件)

- [ ] P3-06: 解决parse-results路由重复
  - 问题描述: POST /parse-results/{id}/apply 同时存在于parse_results.py和tickets.py两个路由中
  - 影响范围: API设计清晰度
  - 推荐方案: 保留一个路由(建议tickets下的), 移除重复定义
  - 涉及文件: backend/app/api/v1/parse_results.py

- [ ] P3-07: 数据库定时备份
  - 问题描述: 当前仅在deploy.sh部署前备份, 无定时备份机制
  - 影响范围: 数据安全性
  - 推荐方案: 添加cron任务定期执行mysqldump, 或使用阿里云RDS自动备份
  - 涉及文件: deploy.sh, 服务器cron配置

---

## 任务依赖关系

- P0-02 → P0-03 → P0-04 (需要.env配置后才能迁移和种子)
- P0-01 → P0-03 (需要MySQL实例存在才能迁移)
- P0-01/P0-02 → P1-01/P1-02/P1-03 (需要运行环境和IMAP/SMTP/OSS配置)
- P1-07 → 前端开发 (需要npm install成功)
- P1-02 → P1-05 (IMAP拉取需要APScheduler调度)
- P2-03 → P2-02 (healthcheck用于Nginx upstream健康感知)

## 任务并行化建议

以下任务可并行执行:
- P1-01(SMTP) + P1-02(IMAP) + P1-03(OSS) — 三个独立的集成模块
- P1-06(Repositories) — 架构重构, 独立于集成模块
- P2-01(deploy.sh) + P2-02(Nginx) + P2-06(日志卷) — 部署配置可并行修改
- P3系列全部可并行
