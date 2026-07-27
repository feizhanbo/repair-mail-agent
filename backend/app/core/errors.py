from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.response import fail


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
