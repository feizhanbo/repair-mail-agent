# Repository Guidelines

## 项目结构与模块职责

后端位于 `backend/app/`：`api/v1/` 只负责参数、鉴权和响应，事务、状态流转放在 `services/`，外部模型放在 `integrations/`，ORM 与 Pydantic 契约分别位于 `models/`、`schemas/`。迁移在 `backend/alembic/versions/`，测试统一放入 `backend/tests/`。

前端位于 `frontend/src/`：页面在 `pages/`，接口统一由 `api/client.ts` 调用，契约集中在 `types/api.ts`，通用逻辑放在 `utils/`。不要在页面中散落 Axios 请求或复制后端业务判断。

## 开发文档查阅顺序

当前事实以代码、ORM、Alembic、测试和前端类型为准，规划能力必须标记为“后续目标”。开发前依次查阅：`docs/00-开发指导文档.md`（原则）、`01-开发进度文档.md`（进度和任务）、`02-数据库详细设计文档.md`（表与迁移）、`03-详细开发细节文档.md`（接口和页面）、`04-项目开发资料文档.md`（业务与 Prompt）、`05-研发与部署规范.md`（环境和部署）。

## 构建、测试与启动命令

```powershell
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
python -m pytest -q
alembic upgrade head

cd ..\frontend
npm install
npm run dev
npm run typecheck
npm run build
```

全栈使用 `docker compose up -d --build`，提交前执行 `git diff --check`。运行迁移前必须确认数据库 URL，远程数据库只经 SSH 隧道做明确授权的验证。

## 编码风格与命名规范

Python 使用四空格、类型注解、`snake_case` 函数变量和 `PascalCase` 类；TypeScript 使用两空格、`camelCase` 值和 `PascalCase` 组件/类型。优先复用现有 service、schema 和组件模式，避免无关重构。仓库未配置统一格式化器，保持改动聚焦。

## 测试与验收要求

pytest 测试命名为 `test_<behavior>`。接口、schema、状态流转、附件类型或权限变化必须补测试。默认 mock IMAP、SMTP、OSS、DeepSeek 和 Qwen；真实集成测试只能在明确范围内执行。验收至少包括后端 pytest、前端 typecheck/build 和应用导入。

## 数据库与文档同步

数据库变更必须同步 ORM、Alembic、测试和 `docs/02`；接口、页面、权限、错误码变化同步 `docs/03` 与前端类型；架构和流程变化同步 `docs/00/01`。邮件链路坚持先做 UID、Message-ID、raw hash 去重和无关筛选，再上传 OSS。当前手工/EML 入口的 OSS 降级是 P0 缺陷，不得写成正式规则。

## Commit 与 Pull Request

使用 `feat:`、`fix:`、`test:`、`docs:`、`refactor:` 加简洁中文描述。PR 说明行为及数据库、接口、权限、AI、前端影响，列出验证命令并关联 issue；UI 变化附截图，行为变化同步对应文档。

## 安全与邮件测试边界

真实凭据只写入已忽略的 `.env`，`.env.example` 只保留占位值。不得记录密钥、附件二进制、敏感邮件正文或完整 OSS 签名 URL。DeepSeek 负责邮件级文本解析，Qwen 负责附件级解析。真实 IMAP 只能访问 `.env` 指定账号；SMTP 只能发送到 `SMTP_RECIPIENT_WHITELIST`，严禁绕过白名单。Relay 仍为占位能力，未配置或开关关闭时禁止发起网络请求。
