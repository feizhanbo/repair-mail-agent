from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import ai_logs, auth, dashboard, db_browser, email_threads, emails, manual_review, master_data, notifications, parse_results, replies, system, tickets, users

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(users.router, prefix="/users", tags=["用户管理"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["首页看板"])
api_router.include_router(emails.router, prefix="/emails", tags=["邮件中心"])
api_router.include_router(email_threads.router, prefix="/email-threads", tags=["邮件线程"])
api_router.include_router(tickets.router, prefix="/tickets", tags=["工单中心"])
api_router.include_router(parse_results.router, prefix="/parse-results", tags=["解析结果"])
api_router.include_router(manual_review.router, prefix="/manual-review", tags=["人工复核"])
api_router.include_router(replies.router, prefix="/replies", tags=["自动回复"])
api_router.include_router(master_data.router, prefix="/master-data", tags=["基础资料"])
api_router.include_router(ai_logs.router, prefix="/ai-logs", tags=["AI 日志"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["通知"])
api_router.include_router(system.router, prefix="/system", tags=["系统配置"])
api_router.include_router(db_browser.router, prefix="/db-browser", tags=["数据库浏览"])
