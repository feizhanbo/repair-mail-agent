# codex-git-01

记录日期：2026-07-02  
最终更新：2026-07-03  
项目目录：`D:\code\repair-mail-agent`  
远程目录：`/root/bert/repair-mail-agent`

## 当前实现基线（2026-07-05）

当前分支目标为 `codex-new-database`。提交前必须先展示暂存文件清单和中文 commit message，等待用户确认后才允许 commit/push。

当前事实：

- `main` 不直接改动。
- 当前代码已完成基础业务、DeepSeek AI、用户管理、站内消息、`parse_results.apply_status` 和接口级 RBAC。
- 远程新库 `repair_system_codex_dev_test` 已完成验证。
- 私有 `.env`、隧道脚本/日志、构建产物、真实密码和 AI key 不提交。

## 1. 本轮目标

1. 创建 GitHub 私有仓库，并让本地与远程服务器后续可通过 Git 迭代同步。
2. 通过 ORM 初始化数据库种子数据。
3. 提交前等待用户确认 commit 详情。
4. 再次验证本地工程、远程服务器与远程 MySQL 的交互。
5. 梳理标准研发与部署文档流程。

## 2. GitHub 与 Git 状态

已完成：

- 本地项目已初始化 Git 仓库，分支为 `main`。
- 已确认 `.env` 被 `.gitignore` 命中，不会被 Git 跟踪。
- 本地 Git remote 已绑定：`origin -> https://github.com/feizhanbo/repair-mail-agent.git`。
- 已按用户确认信息完成初始提交：`c778ad5 chore: 邮件报修自动化系统工程骨架搭建`。
- 已推送到 GitHub 私有仓库：`main -> origin/main`。
- 本地 `main` 已跟踪 `origin/main`，当前工作区干净。
- 远程服务器 `/root/bert/repair-mail-agent` 已初始化 Git 元数据。
- 远程服务器 `origin` 已配置为 `git@github-repair-mail-agent:feizhanbo/repair-mail-agent.git`。
- GitHub 仓库已添加 Deploy key，标题为 `repair-aliyun`。
- 远程服务器已通过 Deploy key 执行 `git fetch origin main` 和 `git pull --ff-only`，当前远程 HEAD 为 `c778ad5`。

曾遇到的问题与处理结果：

- GitHub 连接器当前只暴露已有仓库内的文件、分支、提交、PR/Issue 操作，没有创建新仓库的工具。
- 本机 `gh` 命令不可用，无法通过 GitHub CLI 创建私有仓库。
- 2026-07-02 已收到用户提供的仓库地址 `https://github.com/feizhanbo/repair-mail-agent.git` 并完成本地 `origin` 绑定。
- 只读远程校验 `git ls-remote --heads origin` 返回网络错误：`Recv failure: Connection was reset`。
- GitHub 连接器早期查询 `feizhanbo/repair-mail-agent` 返回 `404 Not Found`；后续已通过私有仓库授权和远程 deploy key 打通 Git 链路。
- 本地 `git push -u origin main` 后执行成功。
- 远程服务器初次通过 HTTPS 拉取私有仓库失败，原因是服务器没有 GitHub HTTPS 凭据。
- 已改为服务器专用 SSH Deploy key；用户在 GitHub 仓库设置 Deploy key 后，远程 `fetch/pull` 已验证通过。
- 远程目录此前由同步包维护，缺少 `.env.example`。接入 Git 后只恢复了公开占位文件 `.env.example`，未覆盖服务器私有 `.env`。

后续标准 Git 流程：

```bash
git remote add origin <private-repo-url>
git add .
git commit
git push -u origin main
```

远程服务器后续切换为：

```bash
cd /root/bert/repair-mail-agent
git pull
```

远程 `.env` 必须保留为服务器私有文件，不通过 Git 覆盖。

## 3. 本轮代码与文档调整

新增：

- `backend/app/seed.py`：ORM 种子数据脚本，支持幂等执行。
- `backend/app/db_smoke.py`：数据库连接、临时写读删、迁移版本和种子计数验证脚本。
- `D:\code\codex-git-01.md`：本轮流程记录。

更新：

- `backend/app/config.py`：支持从当前目录和上级目录读取 `.env`，增加默认管理员种子配置项。
- `backend/requirements.txt`：显式加入 `bcrypt==5.0.0`，种子脚本直接使用 bcrypt 生成密码哈希。
- `.env.example`：补充种子管理员配置占位，并对带空格字段加引号。
- `README.md`：补充 seed、db smoke、本地/远程 Git 更新流程说明。
- `docs/remote-mysql-root-deployment.md`：补充迁移、种子、smoke、Git 更新流程。
- `AI开发进度与任务跟踪.md`：更新当前完成度、验证结果和 GitHub 阻塞项。

私有配置：

- 本地 `.env` 已保存数据库密码和种子管理员密码。
- 远程 `/root/bert/repair-mail-agent/.env` 已保存数据库密码和种子管理员密码。
- 私有 `.env` 文件未写入 Git。

## 4. ORM 种子数据

执行命令：

```bash
cd backend
python -m app.seed
```

执行结果：

```text
workflow_statuses: 8
workflow_transitions: 16
roles: 3
reply_templates: 3
default_admin: admin
```

种子数据范围：

- 流程状态：8 条。
- 状态流转：16 条。
- 角色：3 条。
- 默认管理员：1 个。
- 基础回复模板：3 条。

密码处理：

- 默认管理员密码只从私有 `.env` 读取。
- 文档和 Git 跟踪文件只保留占位值，不记录真实密码。

## 5. 本地工程到远程 MySQL 验证

本地通过 SSH 隧道访问远程 MySQL：

```text
127.0.0.1:13307 -> remote 127.0.0.1:3307 -> repair-mysql:3306
```

验证命令：

```bash
cd backend
python -m app.db_smoke
pytest tests
```

验证结果：

```text
smoke: ok
alembic: 0f2ae6ba263f
tables: 27
workflow_statuses: 8
workflow_transitions: 16
roles: 3
users: 1
reply_templates: 3
```

测试结果：

```text
2 passed
```

说明：`tables: 27` 表示 26 张业务表加 `alembic_version`。

## 6. 远程服务器到远程 MySQL 验证

远程 MySQL 容器信息：

```text
container: repair-mysql
image: mysql:8.0
status: healthy
port: 127.0.0.1:3307 -> 3306/tcp
database: repair_system_dev
account: root
```

远程主机经 `127.0.0.1:3307` 执行 smoke test，结果：

```text
smoke=ok; alembic=0f2ae6ba263f; tables=27; statuses=8; transitions=16; roles=4; users=1; templates=3
```

结论：

- MySQL 容器健康。
- 远程主机可通过本机回环端口访问 MySQL 容器。
- 本地工程可通过 SSH 隧道访问同一个远程 MySQL。
- 当前数据库迁移和种子数据均已生效。

## 7. 标准研发与部署流程梳理

本地开发流程：

1. 启动或确认 SSH 隧道：`127.0.0.1:13307 -> remote 127.0.0.1:3307`。
2. 本地私有 `.env` 使用隧道版 `DATABASE_URL`。
3. 后端本地运行：

```bash
cd backend
python -m compileall app
alembic upgrade head
python -m app.seed
python -m app.db_smoke
pytest tests
uvicorn app.main:app --reload
```

远程部署流程：

1. 远程目录固定为 `/root/bert/repair-mail-agent`。
2. 远程 `.env` 保存在该目录，权限保持 `600`。
3. MySQL 只绑定远程 `127.0.0.1:3307`，不开放公网端口。
4. 远程容器启动：

```bash
docker compose up -d mysql
docker compose ps mysql
```

5. 远程 smoke 通过临时 MySQL 客户端或主机 MySQL 客户端访问 `127.0.0.1:3307`。

Git 化后的部署更新流程：

1. GitHub private repository 创建完成。
2. 本地提交并推送 `main`。
3. 远程服务器通过 `git pull` 或 CI/CD 更新非私有代码。
4. 远程 `.env` 不纳入 Git，不被覆盖。
5. 更新后执行 `docker compose up -d --build` 或按服务粒度重启。

## 8. 最终验证结果

本地 Git：

```text
## main...origin/main
c778ad5 chore: 邮件报修自动化系统工程骨架搭建
```

远程 Git：

```text
## main...origin/main
Already up to date.
c778ad5
```

本地工程到远程 MySQL：

```text
smoke: ok
alembic: 0f2ae6ba263f
tables: 27
workflow_statuses: 8
workflow_transitions: 16
roles: 3
users: 1
reply_templates: 3
```

远程服务器到远程 MySQL：

```text
container: repair-mysql healthy
remote temp-table smoke: ok
alembic=0f2ae6ba263f
statuses=8
transitions=16
roles=4
users=1
templates=3
```

远程私有配置：

```text
/root/bert/repair-mail-agent/.env exists
permission: 600
```

结论：本地、GitHub 私有仓库、远程服务器 Git 工作区、远程 MySQL 容器和本地隧道联调流程均已打通。后续标准迭代流程为本地开发提交到 GitHub，远程服务器在 `/root/bert/repair-mail-agent` 执行 `git pull --ff-only` 更新代码，私有 `.env` 始终留在服务器本地。
