from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    success: bool
    data: T | None = None
    message: str = "ok"
    request_id: str


class PageData(BaseModel):
    items: list[Any]
    total: int
    page: int = 1
    page_size: int = 20

