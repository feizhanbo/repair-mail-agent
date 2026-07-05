# 远程服务器 Docker 容器化开发部署执行顺序说明

生成日期：2026-07-01  
适用项目：邮件报修自动化系统  
前置状态：远程服务器已连通，详细开发文档已落实，最终运行环境为远程服务器 Docker 容器

## 当前实现基线（2026-07-05）

本文件保留远程 Docker 容器化开发部署顺序说明。当前代码阶段以本地开发和验证为主：

- 当前分支目标为 `codex-new-database`。
- 当前数据库结构为 26 张业务表 + `alembic_version`，最新迁移 `9d2b7c4f1a30`。
- 当前角色为 `admin/supervisor/operator`。
- 远程新库 `repair_system_codex_dev_test` 已完成迁移和用户新增展示删除验证。
- 本轮不执行部署，不对既有业务库执行迁移、seed、db_smoke 或写入型验证。
- 真实数据库口令、AI key、邮箱和 OSS 凭据不写入文档或 Git。

## 1. 结论

结合当前项目状态，推荐采用“本地/仓库先补齐工程骨架，远程先部署 MySQL 基础容器，再逐步接入后端、前端、Nginx、CI/CD”的顺序。

不建议一开始就把远程服务器上所有应用容器一次性部署完成。本文早期用于指导从无到有的部署顺序；截至 2026-07-03，工程骨架、前端、后端、Compose、Nginx、GitHub Actions、迁移和 GitHub 私有仓库链路已建立，后续重点转为业务模块实现。

也不建议长期先用本地 MySQL 开发完整功能后再迁移远程 MySQL。项目最终数据库明确使用远程服务器 Docker 容器化 MySQL，早期就应使用远程 MySQL 作为开发/集成基线，尽早验证字符集、时区、连接方式、权限、容器网络、迁移脚本和备份恢复流程。

推荐执行主线：

```text
工程骨架与文档基线
-> 远程 Docker MySQL 基础容器
-> 数据库连接与 Alembic 迁移
-> 后端 API 容器
-> 前端工程与静态构建
-> Nginx 统一入口
-> GitHub Actions 自动部署
-> 按业务模块迭代开发
```

## 2. 当前项目状态分析

### 2.1 已具备

- 已有 PRD 技术方案。
- 已有数据库一期最终版，明确一期 26 张表。
- 已有详细开发设计文档：`邮件报修自动化系统详细开发设计文档.md`。
- 已有标准研发部署流程：`Codex_Docker_CICD_标准研发与部署流程.md`。
- 已有 README、AI 开发进度文档、统一枚举清单。
- 已有后端、前端、Docker Compose、Nginx、GitHub Actions、部署脚本初版。
- 已有一期 26 张表 ORM 和 Alembic 迁移。
- 已有远程 Docker MySQL、root 开发账号、`repair_system_dev` 开发库和本地 SSH 隧道。
- 已有 GitHub 私有仓库、远程 deploy key 和远程 `git pull --ff-only` 更新链路。

### 2.2 当前缺口

- 认证、邮件、工单、人工复核、回复、基础资料、AI 日志、通知等业务 API 仍为占位。
- 真实 IMAP/SMTP/OSS/AI 配置尚未提供并联调。
- `deploy.sh` 仍需补充迁移、备份、seed、健康检查等上线门禁。
- 真实邮件回归测试和完整业务验收尚未执行。

因此当前最优做法是在已完成的工程骨架、远程 MySQL、迁移体系和 Git 链路上，继续按业务模块迭代。

## 3. 总体执行原则

### 3.1 远程 MySQL 作为唯一开发基线数据库

数据库使用远程服务器 Docker 容器中的 MySQL 8.x。当前开发期使用 root 账号和 `repair_system_dev`，SQL 地址以当前实际远程环境和本地 SSH 隧道为准。

本地开发访问远程 MySQL 时，不开放公网 3306，使用 SSH 隧道：

```powershell
ssh -N -L 13307:127.0.0.1:3307 <server-alias>
```

本地后端 `.env` 使用：

```text
DATABASE_URL=mysql+asyncmy://root:<ROOT_PASSWORD>@127.0.0.1:13307/repair_system_dev
```

远程 Docker Compose 内部运行后端时使用：

```text
DATABASE_URL=mysql+asyncmy://root:<ROOT_PASSWORD>@mysql:3306/repair_system_dev
```

### 3.2 不直接暴露数据库公网端口

MySQL 端口只能用于：

- Docker 内部网络访问。
- 服务器本机 `127.0.0.1`。
- SSH 隧道访问。

安全组和防火墙不开放 MySQL `3306` 到公网。

### 3.3 应用容器逐步接入

容器接入顺序：

1. `mysql`
2. 后端迁移/初始化任务
3. `backend-api`
4. `frontend`
5. `nginx`
6. GitHub Actions 自动部署

数据库容器可以先行部署；后端、前端、Nginx 应在工程骨架和配置文件稳定后再接入。

### 3.4 所有部署最终必须走标准流程

按照 `Codex_Docker_CICD_标准研发与部署流程.md`：

- 所有服务必须 Docker 化。
- 所有外部请求必须通过 Nginx。
- 所有部署必须通过 GitHub Actions。
- 禁止在生产/试运行环境手工改代码。
- 所有日志必须可追踪。
- 所有服务必须可重建。

开发早期允许手动执行 Docker Compose 验证基础环境；一旦 GitHub Actions 建立，后续部署必须通过 CI/CD。

## 4. 推荐执行顺序

### 阶段 0：远程服务器基础检查

目标：确认远程服务器具备容器化运行条件。

执行位置：远程服务器。

检查项：

```bash
docker --version
docker compose version
git --version
df -h
free -h
```

需要确认：

- Docker 服务已启动。
- 当前用户可执行 Docker 命令。
- 项目目录已准备，例如 `/opt/refile`。
- 安全组只开放 SSH、HTTP、HTTPS。
- 不开放 MySQL 3306/3307、后端 8000、前端 5173 到公网。

验收标准：

- 可以在服务器执行 `docker compose version`。
- 项目目录可读写。
- 本机可 SSH 登录服务器。

### 阶段 1：先在仓库补齐工程骨架

目标：让项目具备可 Docker 化、可迁移、可部署的基础文件结构。

执行位置：本地工作区或远程项目目录均可，但最终必须进入 Git 仓库。

本阶段应创建或补齐：

```text
README.md
AI开发进度与任务跟踪.md
infra/docker-compose.yml 或 docker-compose.yml
nginx/default.conf
deploy.sh
.github/workflows/deploy.yml
frontend/
backend/.env.example
```

说明：

- `README.md` 用于团队了解业务、结构、配置、启动、部署。
- `AI开发进度与任务跟踪.md` 用于后续 AI 回顾项目方向和任务状态。
- `docker-compose.yml` 初期可以只包含 `mysql`，随后再增加 `backend-api`、`frontend`、`nginx`。
- 不能先写业务功能再补这些文件，因为后续所有开发都要围绕 Docker、远程 MySQL 和 CI/CD 流程推进。

验收标准：

- 仓库能清楚说明如何启动项目。
- AI 进度文档记录当前真实状态：后端初稿、前端未建、MySQL 待部署、迁移待写。
- Docker Compose 文件至少具备 MySQL 服务定义的落点。

### 阶段 2：远程部署 MySQL 容器

目标：先建立最终一致的数据库运行环境。

执行位置：远程服务器。

推荐 MySQL Compose 设计：

```yaml
services:
  mysql:
    image: mysql:8.0
    container_name: repair-mysql
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
      MYSQL_DATABASE: repair_system_dev
      TZ: Asia/Shanghai
    command:
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_unicode_ci
      - --default-time-zone=+08:00
    ports:
      - "127.0.0.1:3307:3306"
    volumes:
      - mysql_data:/var/lib/mysql
    networks:
      - repair_net
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1"]
      interval: 10s
      timeout: 5s
      retries: 10

volumes:
  mysql_data:

networks:
  repair_net:
```

注意：

- `ports` 当前绑定 `127.0.0.1:3307:3306`，只允许服务器本机和 SSH 隧道访问。
- 应用容器访问 MySQL 时使用 `mysql:3306`，不走宿主机端口。
- 当前开发期使用 root 账号；后续如切换业务账号，必须同步 `.env`、README、CI/CD Secrets 和远程部署记录。
- `.env` 文件只保存在服务器，不提交 Git。

启动：

```bash
docker compose up -d mysql
docker logs -f repair-mysql
```

验收：

```bash
docker exec -it repair-mysql mysql -uroot -p repair_system_dev
```

进入 MySQL 后检查：

```sql
SHOW DATABASES;
SHOW VARIABLES LIKE 'character_set_server';
SHOW VARIABLES LIKE 'collation_server';
SELECT @@time_zone;
```

验收标准：

- MySQL 容器健康。
- 数据库 `repair_system_dev` 存在。
- 字符集为 `utf8mb4`。
- 排序规则为 `utf8mb4_unicode_ci` 或 PRD 允许的 MySQL 8 稳定排序规则。
- 本地可通过 SSH 隧道连接。

### 阶段 3：打通后端到远程 MySQL 的连接

目标：让后端开发和迁移脚本使用远程容器化 MySQL。

执行位置：本地或远程均可。

本地开发连接方式：

```powershell
ssh -L 3307:127.0.0.1:3306 aliyun-repair
```

本地 `.env`：

```text
DATABASE_URL=mysql+asyncmy://root:<ROOT_PASSWORD>@127.0.0.1:13307/repair_system_dev
```

远程 Docker 后端容器 `.env`：

```text
DATABASE_URL=mysql+asyncmy://root:<ROOT_PASSWORD>@mysql:3306/repair_system_dev
```

要修改的代码/配置：

- `backend/.env.example`：补充本地隧道和容器内两种连接示例。
- `backend/app/config.py`：保持通过环境变量读取，不在代码中写死远程 IP。
- `README.md`：写清楚本地隧道、远程容器连接区别。
- `AI开发进度与任务跟踪.md`：记录 MySQL 容器是否已部署、连接是否打通。

验收标准：

- 后端能通过 `DATABASE_URL` 连接远程 MySQL。
- 连接字符串不包含 root 账号。
- 真实密码不进入仓库。

### 阶段 4：实现并执行一期 26 表 Alembic 迁移

目标：把 PRD 和数据库一期最终版落到远程 MySQL。

执行位置：建议先本地通过 SSH 隧道执行；后续改为在后端容器中执行。

开发内容：

- 按 `邮件报修自动化系统数据库表字段设计方案_一期最终版.md` 创建 26 张表 ORM。
- 修正早期模型命名：
  - `tickets` -> `repair_tickets`
  - `attachments` -> `email_attachments`
  - `sn_library` -> `sn_assets`
  - `auto_replies` -> `reply_records`
  - `audit_logs` -> `operation_logs` 或 `field_audit_logs`
  - `system_logs` -> `system_event_logs`
  - `ai_execution_logs` -> `ai_call_logs`
- 创建 Alembic migration。
- 初始化种子数据：
  - `roles`
  - `workflow_statuses`
  - `workflow_transitions`
  - 默认管理员
  - 基础回复模板

执行：

```bash
cd backend
alembic upgrade head
```

验证：

```sql
SHOW TABLES;
SELECT COUNT(*) FROM workflow_statuses;
SELECT COUNT(*) FROM roles;
```

验收标准：

- 26 张一期表全部存在。
- 索引、唯一约束、外键符合一期最终版。
- 初始状态和角色存在。
- Alembic 可重复识别当前版本。

### 阶段 5：部署后端 API 容器

目标：让后端在远程服务器 Docker 容器中运行，并通过容器网络访问 MySQL。

执行位置：远程服务器。

Compose 增加：

```yaml
services:
  backend-api:
    build:
      context: ./backend
    container_name: repair-backend-api
    restart: unless-stopped
    env_file:
      - .env
    depends_on:
      mysql:
        condition: service_healthy
    networks:
      - repair_net
    expose:
      - "8000"
```

注意：

- 后端容器不直接映射公网端口。
- 外部访问后续统一走 Nginx。
- 数据库连接使用 `mysql:3306`。
- 启动后先验证 `/health`。

启动：

```bash
docker compose up -d --build backend-api
docker logs -f repair-backend-api
```

验收：

```bash
docker exec -it repair-backend-api python -c "from app.config import settings; print(settings.DATABASE_URL)"
```

通过服务器本机或临时 curl 验证：

```bash
curl http://127.0.0.1:8000/health
```

若后端不映射端口，可通过容器网络或临时调试方式验证。

验收标准：

- 后端容器启动成功。
- 后端连接 MySQL 成功。
- `/health` 返回成功。
- Docker logs 无启动异常。

### 阶段 6：创建并接入前端工程

目标：建立 React + TypeScript + Ant Design 控制台工程。

执行位置：本地工作区开发，远程容器部署验证。

开发内容：

- 创建 `frontend/`。
- 配置 Vite、TypeScript、Ant Design、React Router、TanStack Query、Zustand。
- 实现基础布局、登录页、看板占位、接口 client。
- 配置生产构建输出。
- 创建前端 Dockerfile，构建静态资源。

本阶段不要求一次性完成所有业务页面，但必须形成可构建、可部署、可通过 Nginx 访问的前端底座。

验收：

```bash
npm run build
```

或容器构建：

```bash
docker compose build frontend
```

验收标准：

- 前端工程可构建。
- API base URL 通过环境变量配置。
- 登录页和基础布局可显示。

### 阶段 7：接入 Nginx 统一入口

目标：满足标准研发部署流程中“所有请求必须通过 Nginx”的约束。

推荐路由：

```text
/        -> frontend 静态资源
/api/    -> backend-api:8000
/health  -> backend-api:8000/health 或 Nginx 自身健康检查
```

Nginx 配置要点：

```nginx
server {
    listen 80;

    location /api/ {
        proxy_pass http://backend-api:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location / {
        root /usr/share/nginx/html;
        try_files $uri /index.html;
    }

    access_log /var/log/nginx/access.log;
    error_log /var/log/nginx/error.log;
}
```

Compose 增加：

```yaml
services:
  nginx:
    image: nginx:stable
    container_name: repair-nginx
    restart: unless-stopped
    ports:
      - "80:80"
    depends_on:
      - backend-api
    networks:
      - repair_net
```

验收标准：

- 浏览器访问服务器公网 IP 能打开前端。
- `/api/v1/...` 请求能转发到后端。
- 后端 8000 不直接暴露公网。
- Nginx access/error logs 可查看。

### 阶段 8：接入 GitHub Actions 与部署脚本

目标：从手动远程部署切换为标准 CI/CD 部署。

开发内容：

- 创建 `.github/workflows/deploy.yml`。
- 创建 `deploy.sh`。
- 配置 GitHub Secrets：
  - `SERVER_HOST`
  - `SERVER_USER`
  - `SERVER_SSH_KEY`
  - `SERVER_PORT`
  - 可选：部署目录。
- 部署脚本执行：
  - 拉取最新代码。
  - 备份数据库。
  - `docker compose down`。
  - `docker compose up -d --build`。
  - 执行迁移。
  - 检查健康状态。

注意：

- 试运行数据出现后，部署前必须备份 MySQL。
- 迁移失败必须停止部署并保留旧容器。
- 不允许在服务器手工改代码绕过 GitHub。

验收标准：

- push 到指定分支后自动部署。
- 部署完成后 Nginx 可访问。
- Docker logs 和 Nginx logs 可查看。
- 失败时能定位到 CI 日志。

### 阶段 9：按业务模块迭代开发

目标：在稳定容器底座上逐步实现业务能力。

推荐模块顺序：

1. 认证、用户、角色、统一响应、错误处理。
2. 邮件入库模型和邮件列表 API。
3. IMAP 拉取、EML/附件 OSS 存储。
4. 邮件线程归并。
5. 解析结果 `parse_results`。
6. SN/板卡基础资料导入。
7. 工单生成与状态机。
8. 人工复核任务与通知。
9. 自动回复草稿与审核。
10. AI Provider 和 AI 日志。
11. 前端核心页面联调。
12. 真实邮件回归测试。

每个模块结束都必须：

- 更新 `AI开发进度与任务跟踪.md`。
- 更新 `README.md` 中相关命令或配置。
- 补测试。
- Docker 验证。
- 通过 GitHub Actions 部署验证。

## 5. 两种执行方案对比

### 5.1 方案 A：先本地生成工程骨架，再部署远程 MySQL，然后逐步接入应用容器

推荐采用。

优点：

- 符合项目从工程骨架、远程 MySQL、迁移体系逐步接入应用容器的演进方式。
- 能尽早使用最终形态的远程 Docker MySQL。
- 不会在远程服务器上堆一批无意义占位容器。
- 后续迁移、后端、前端、Nginx、CI/CD 都能围绕同一个 Docker 网络逐步演进。
- 风险可控，每一步都有验收标准。

缺点：

- 前期需要先补齐项目骨架和基础文档。
- 本地连接远程 MySQL 需要 SSH 隧道或远程开发环境。

### 5.2 方案 B：先把远程服务器所有 Docker 容器部署完成，再开发项目骨架

不推荐。

问题：

- 当前没有完整前端、Nginx、CI/CD 和数据库迁移，全量容器无法真正运行完整业务。
- 后端 ORM 还未对齐 PRD 表名，过早部署会产生错误表或空容器。
- 后续反复调整 compose、网络、卷、环境变量，容易造成远程环境混乱。
- 容易把“容器启动成功”误认为“项目架构完成”。

仅适用于已有完整工程骨架和镜像构建脚本的项目；当前项目不符合。

### 5.3 方案 C：先完全本地开发，最后迁移到远程 Docker MySQL

不推荐长期采用。

问题：

- 本地数据库环境与远程 Docker MySQL 差异会延后暴露。
- 字符集、时区、权限、连接池、容器网络、迁移脚本可能上线前才发现问题。
- 最终目标是远程容器化部署，过晚接入远程 MySQL 会增加迁移风险。

可接受的短期场景：

- 编写纯函数、前端静态页面、无数据库依赖组件。
- 写单元测试时使用 mock 或临时测试数据库。

但凡涉及 ORM、迁移、接口联调、状态机和真实数据，都应尽早接入远程 MySQL。

## 6. 推荐下一步立即执行清单

按当前状态，下一轮开发建议执行：

1. 创建根级 `README.md`。
2. 创建根级 `AI开发进度与任务跟踪.md`，记录当前真实状态。
3. 创建 `docker-compose.yml` 或 `infra/docker-compose.yml`，先定义 `mysql`、网络、卷和健康检查。
4. 在远程服务器创建 `.env`，写入 MySQL root 密码、业务账号密码、数据库名。
5. 远程启动 MySQL 容器。
6. 本地通过 SSH 隧道验证连接。
7. 修改 `backend/.env.example`，写清本地隧道和容器内连接方式。
8. 按一期最终版重构后端 ORM 表名。
9. 创建 Alembic 首个迁移，生成 26 张一期表。
10. 在远程 MySQL 执行迁移和种子数据初始化。
11. 再接入后端 API 容器。
12. 再创建前端工程和 Nginx。
13. 最后建立 GitHub Actions 自动部署。

最关键的前三步是：

```text
README + AI进度文档
-> docker-compose MySQL 基础容器
-> Alembic 26 表迁移
```

没有完成这三步前，不建议展开大量业务页面或复杂 AI 逻辑开发。

## 7. 数据库数据衔接策略

### 7.1 开发期

- 使用远程 Docker MySQL 中的开发数据库。
- 初期可反复重建表，但必须通过 Alembic 管理。
- 测试数据通过 seed 脚本或导入接口进入，不手工随意改库。

### 7.2 试运行期

- 数据库卷 `mysql_data` 视为正式持久数据。
- 每次部署前执行备份。
- 迁移前先备份，迁移失败必须回滚或恢复。
- 禁止删除真实邮件、附件引用、审计日志。

### 7.3 备份建议

备份命令示例：

```bash
docker exec repair-mysql mysqldump -uroot -p repair_system_dev > backup/repair_system_dev_$(date +%Y%m%d_%H%M%S).sql
```

恢复命令示例：

```bash
docker exec -i repair-mysql mysql -uroot -p repair_system_dev < backup/xxx.sql
```

真实执行时不要把密码写进命令历史，可使用交互输入或 Docker secret/环境变量方式。

## 8. 环境变量分层

建议分三类：

### 8.1 本地开发 `.env.local`

用途：本机通过 SSH 隧道访问远程 MySQL。

```text
DATABASE_URL=mysql+asyncmy://root:<ROOT_PASSWORD>@127.0.0.1:13307/repair_system_dev
AUTO_SEND_ENABLED=false
```

### 8.2 远程服务器 `.env`

用途：Docker Compose 读取。

```text
MYSQL_ROOT_PASSWORD=<ROOT_PASSWORD>
DATABASE_URL=mysql+asyncmy://root:<ROOT_PASSWORD>@mysql:3306/repair_system_dev
AUTO_SEND_ENABLED=false
```

### 8.3 GitHub Actions Secrets

用途：自动部署。

```text
SERVER_HOST
SERVER_USER
SERVER_PORT
SERVER_SSH_KEY
```

敏感信息不进入 Git 仓库。

## 9. 风险与控制

| 风险 | 控制方式 |
| --- | --- |
| MySQL 暴露公网 | 端口只绑定 `127.0.0.1`，安全组不开放 3306 |
| ORM 表名与 PRD 不一致 | 先重构模型和迁移，再开发业务 API |
| 远程环境被手工改乱 | 尽快接入 GitHub Actions，禁止生产手工改代码 |
| 迁移破坏数据 | 迁移前备份，保留 Alembic 回滚脚本 |
| 自动回复误发客户 | `AUTO_SEND_ENABLED=false`，一期默认人工审核 |
| 容器启动但业务不可用 | 每阶段设置健康检查和业务验收标准 |
| AI 开发偏离方向 | 每轮开发前后维护 `AI开发进度与任务跟踪.md` |
| 附件/EML 丢失 | 文件进 OSS，数据库保存对象元数据和 hash |

## 10. 最终目标运行形态

最终远程服务器应形成：

```text
Docker Compose
  ├── nginx               # 唯一公网入口，80/443
  ├── frontend            # React 构建产物或静态资源镜像
  ├── backend-api         # FastAPI 服务
  ├── mysql               # MySQL 8.x，持久卷
  └── 日志与审计          # Docker logs、Nginx logs、数据库日志表
```

访问路径：

```text
用户浏览器 -> Nginx -> 前端静态页面
用户浏览器 -> Nginx /api -> backend-api
backend-api -> Docker 内部网络 -> mysql
backend-api -> OSS / IMAP / SMTP / AI Provider
```

外部只开放：

- SSH：用于管理和 CI/CD。
- HTTP/HTTPS：用于访问系统。

不开放：

- MySQL 3306。
- 后端 8000。
- 前端开发端口 5173。

## 11. 执行顺序总览

```text
0. 检查远程服务器 Docker/Git/磁盘/内存
1. 本地或仓库补齐 README、AI 进度文档、compose 初稿
2. 远程启动 MySQL 容器，创建持久卷和业务账号
3. 本地通过 SSH 隧道连接远程 MySQL
4. 后端配置 DATABASE_URL，区分本地隧道和容器内连接
5. 按 PRD + 一期最终版创建 26 表 ORM 和 Alembic 迁移
6. 执行迁移和种子数据初始化
7. 部署 backend-api 容器并验证 /health
8. 创建 frontend 工程并完成生产构建
9. 接入 Nginx，统一暴露 80/443
10. 接入 GitHub Actions 和 deploy.sh
11. 按业务模块持续迭代
12. 每轮更新 README 和 AI 开发进度文档
```

一句话原则：先让“工程骨架 + 远程容器化 MySQL + 迁移体系”站稳，再让后端、前端、Nginx 和 CI/CD 一层一层接上去。
