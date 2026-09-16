from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.response import fail


# --- 删除能力错误码（字符串常量，与现有 USER_NOT_FOUND 等风格一致） ---
# 邮件删除
EMAIL_NOT_FOUND = "EMAIL_NOT_FOUND"
EMAIL_BEING_PROCESSED = "EMAIL_BEING_PROCESSED"  # processing_stage in (fetched/parsing/classifying)
EMAIL_PARSE_PENDING = "EMAIL_PARSE_PENDING"  # parse_status == pending

# 工单删除
TICKET_NOT_FOUND = "TICKET_NOT_FOUND"
TICKET_IN_EXTERNAL_SYSTEM = "TICKET_IN_EXTERNAL_SYSTEM"  # ready_for_export / rma_sent
RMA_ALREADY_ISSUED = "RMA_ALREADY_ISSUED"  # rma_status in (issued, sent)
SAP_ALREADY_SYNCED = "SAP_ALREADY_SYNCED"  # relay_export_status in (exported, accepted)
EMAIL_ALREADY_SENT = "EMAIL_ALREADY_SENT"  # reply_records.send_status == sent
TICKET_VERSION_CONFLICT = "TICKET_VERSION_CONFLICT"  # 乐观锁冲突

# 附件删除
ATTACHMENT_NOT_FOUND = "ATTACHMENT_NOT_FOUND"
ATTACHMENT_IN_USE = "ATTACHMENT_IN_USE"  # 被 parse_results/ai_call_logs 引用

# OSS 删除
OSS_OBJECT_NOT_FOUND = "OSS_OBJECT_NOT_FOUND"
OSS_OBJECT_IN_USE = "OSS_OBJECT_IN_USE"  # 引用计数 > 0
OSS_DELETE_FAILED = "OSS_DELETE_FAILED"  # SDK 调用失败（仅日志，不抛出）


def _detail_code(detail: object, default: str) -> str:
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return detail["code"]
    return default


def _detail_data(detail: object) -> Any:
    if isinstance(detail, dict):
        return detail.get("data")
    return None


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    code = _detail_code(exc.detail, f"HTTP_{exc.status_code}")
    return JSONResponse(status_code=exc.status_code, content=fail(code=code, message=code, data=_detail_data(exc.detail)))


async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=fail(code="REQUEST_VALIDATION_ERROR", message="REQUEST_VALIDATION_ERROR", data={"errors": exc.errors()}),
    )


async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    import traceback, logging
    logger = logging.getLogger("app.errors")
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=fail(code="INTERNAL_SERVER_ERROR", message="INTERNAL_SERVER_ERROR"),
    )
