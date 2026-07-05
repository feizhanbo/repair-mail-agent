from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder


def request_id() -> str:
    return f"req_{uuid4().hex[:16]}"


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {
        "success": True,
        "data": jsonable_encoder(data if data is not None else {}),
        "message": message,
        "request_id": request_id(),
    }


def page(items: list[Any], total: int, page_no: int = 1, page_size: int = 20) -> dict[str, Any]:
    return ok({"items": items, "total": total, "page": page_no, "page_size": page_size})


def fail(code: str, message: str | None = None, data: Any = None) -> dict[str, Any]:
    return {
        "success": False,
        "data": jsonable_encoder(data if data is not None else {}),
        "message": message or code,
        "request_id": request_id(),
    }

