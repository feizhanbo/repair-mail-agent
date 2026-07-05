# 开发依据

更新日期：2026-07-05

## 当前事实源

当前开发以以下文件作为优先事实源：

1. `README.md`
2. `AI开发进度与任务跟踪.md`
3. `docs/alignment-audit/*.md`

上述文件记录当前代码事实：基础业务闭环、DeepSeek AI、`parse_results.apply_status`、用户管理、个人信息、站内消息、接口级 RBAC、远程新库验证结果。

## 历史依据文档

以下文档仍作为业务背景、数据库设计、部署流程和开发规范参考：

- `邮件报修自动化系统PRD技术方案.md`
- `邮件报修自动化系统详细开发设计文档.md`
- `邮件报修自动化系统数据库表字段设计方案_一期最终版.md`
- `邮件报修自动化系统统一开发总结文档.md`
- `远程服务器Docker容器化开发部署执行顺序说明.md`
- `Codex_Docker_CICD_标准研发与部署流程.md`
- `codex-git-01.md`
- `docs/remote-mysql-root-deployment.md`

## 冲突处理

如历史文档与当前代码事实冲突，以当前事实源为准。当前已明确废弃或更新的旧内容包括：

- 角色不再使用 `viewer`，统一为 `admin/supervisor/operator`。
- `parse_results.apply_status/applied_by_user_id/applied_at` 已落地，不再是缺口。
- 远程新库验证已完成，不再以“users 为空”为当前登录阻塞结论。
- 高危接口已接入 RBAC，不再是“只登录不分角色”的状态。

真实数据库口令、AI key、邮箱凭据、OSS key 不进入任何开发文档。
