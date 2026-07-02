from __future__ import annotations

from typing import Any
from uuid import uuid4


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {
        "success": True,
        "data": data if data is not None else {},
        "message": message,
        "request_id": f"req_{uuid4().hex[:16]}",
    }


def page(items: list[Any], total: int, page_no: int = 1, page_size: int = 20) -> dict[str, Any]:
    return ok({"items": items, "total": total, "page": page_no, "page_size": page_size})

