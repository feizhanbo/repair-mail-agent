> ⚠️ 本文档为历史参考，不可作为开发依据。最新信息请查阅 docs/ 目录下的正式文档。
> 归档日期：2026-07-13
> 替代文档：docs/05-研发与部署规范.md

---

# Codex + Docker + CI/CD 标准研发与部署流程

## 当前实现基线（2026-07-05）

本文件作为 Docker、CI/CD 和部署流程参考。当前开发分支工作只做本地代码、测试和文档同步；提交和推送必须等待用户确认暂存清单和中文 commit。

- 当前业务基线：26 张业务表 + `alembic_version`，最新迁移 `9d2b7c4f1a30`。
- 当前角色：`admin/supervisor/operator`。
- 远程新库验证库：`repair_system_codex_dev_test`。
- 本轮不执行部署，不执行新的数据库迁移、seed、db_smoke 或真实业务库写入。
- 真实 `.env`、数据库口令、AI key、邮箱和 OSS 凭据不得进入 Git。

## 1. 总体目标

本体系用于构建统一的远程开发与部署流程，实现：

- Codex 负责开发，也就是编写和修改代码。
- GitHub 作为代码中心。
- Docker 作为运行环境标准化工具。
- Nginx 作为统一入口。
- CI/CD 实现自动部署。
- 日志系统实现可观测性。

## 2. 系统架构

```text
开发端（Codex）
      ↓
GitHub（代码仓库）
      ↓
GitHub Actions（CI/CD）
      ↓
Linux 服务器（生产环境）
      ↓
Docker Compose
   ├── Nginx（反向代理入口）
   ├── Python API（业务服务）
   └── 日志系统（Docker logs + Nginx logs）
```

## 3. 标准项目结构

所有项目必须采用以下结构：

```text
project/
│
├── app/                        # 业务代码（Python API）
│   ├── main.py
│   ├── requirements.txt
│
├── nginx/                      # 网关配置
│   └── default.conf
│
├── docker/                     # Docker 构建文件
│   └── Dockerfile
│
├── docker-compose.yml          # 服务编排
├── deploy.sh                   # 本地手动部署脚本
└── .github/workflows/
    └── deploy.yml              # CI/CD 自动部署
```

## 4. 标准开发流程

### Step 1：开发（Codex）

- 编写 API / 业务逻辑。
- 本地或 Docker 中验证。

### Step 2：提交代码

```bash
git add .
git commit -m "feature: update service"
git push origin main
```

### Step 3：自动触发 CI/CD

GitHub Actions 自动执行：

- 拉取最新代码。
- SSH 登录服务器。
- 执行部署脚本。

### Step 4：服务器自动部署

```bash
git pull origin main
docker compose down
docker compose up -d --build
```

### Step 5：服务对外发布

统一入口：

```text
http://server-ip/
```

通过 Nginx 转发到 Python API。

### Step 6：日志与监控运行

- API logs：Docker logs。
- Nginx logs：access/error logs。
- 系统指标：当前邮件报修自动化项目一期不接入 Prometheus/Node Exporter。

## 5. Docker 标准规范

### Python API Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY app /app

RUN pip install -r requirements.txt

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Docker Compose 标准结构

必须包含：

- Python API。
- Nginx。

当前邮件报修自动化项目不使用 Prometheus、Node Exporter 或 Redis；一期以 Docker logs、Nginx logs 和数据库日志审计表满足基础排障。

## 6. Nginx 标准入口

```nginx
server {
    listen 80;

    location / {
        proxy_pass http://python-api:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    access_log /var/log/nginx/access.log;
    error_log /var/log/nginx/error.log;
}
```

## 7. CI/CD 自动部署

### GitHub Actions 配置

触发条件：

```yaml
on:
  push:
    branches:
      - main
```

执行流程：

- checkout code。
- SSH login server。
- git pull latest code。
- docker compose rebuild。
- restart services。

## 8. 自动部署脚本

`deploy.sh`：

```bash
#!/bin/bash

echo "sync code..."
rsync -av ./ user@server:/home/user/project/

ssh user@server << 'EOF'
cd /home/user/project

echo "rebuilding services..."
docker compose down
docker compose up -d --build

echo "deployment completed"
EOF
```

## 9. 标准运行流程

日常开发循环：

1. Codex 写代码。
2. git push。
3. 自动部署触发。
4. 服务更新上线。

## 10. 日志系统规范

查看 API 日志：

```bash
docker logs -f python-api
```

查看 Nginx 日志：
  

```bash
tail -f logs/nginx/access.log
```

## 12. 热更新机制

开发模式：

```bash
uvicorn main:app --reload
```

Docker 挂载：

```yaml
volumes:
  - ./app:/app
```

## 13. 系统核心思想

- Codex = 代码生成器。
- GitHub = 控制中心。
- Docker = 环境标准化。
- CI/CD = 自动交付。
- Nginx = 流量入口。
- Logs = 系统观察能力。

## 14. 强制约束

- 所有服务必须 Docker 化。
- 所有请求必须通过 Nginx。
- 所有部署必须通过 GitHub Actions。
- 禁止手动生产环境修改代码。
- 所有日志必须可追踪。
- 所有服务必须可重建（stateless）。

## 15. 后续执行原则

后续所有远程服务器开发工作，均以本文档作为默认研发与部署规范。若实际服务器环境与规范存在冲突，优先调整环境或补充迁移计划，而不是绕过标准流程直接手工部署。
